"""Client for Skynamo's Reporting (Analytics) API.

Separate from client.SkynamoClient: different host, OAuth2 client-credentials
instead of x-api-key headers, and read-only. Shaped deliberately like
SkynamoClient - a requests.Session, REQUEST_TIMEOUT, (value, error) returns -
so front-ends drive both the same way.

Two things this handles that callers should not have to think about:

  * Rate limits. They are published, tight, and scale inversely with the
    reporting period (AllData is 2 queries per 10 minutes). The client
    self-throttles per period, like NominatimGeocoder does for its 1 req/s.
  * Tokens. Lifetime is undocumented, so the cache trusts `expires_in` and
    refreshes once on a 401 rather than assuming any fixed lifetime.

Credentials are never logged, never persisted, and never included in an
exception message.
"""

import json
import time

import requests

from .config import REQUEST_TIMEOUT
from .reporting_config import (
    ANALYTICS_BASE, DEFAULT_RATE_LIMIT, FILTERABLE_FIELDS_ENDPOINTS,
    RATE_LIMIT_BY_PERIOD, REPORTING_ENTITIES, ROLES_ENDPOINT,
    TOKEN_AUDIENCE, TOKEN_DEFAULT_TTL, TOKEN_REFRESH_SKEW, TOKEN_URL,
)


class ReportingError(Exception):
    """Raised for fatal problems (bad credentials, persistent throttling)."""


class TokenCache:
    """Cache a bearer token and refresh shortly before it expires.

    Lifetime is undocumented, so trust `expires_in` from the response and fall
    back to a short default rather than assuming a long one.
    """

    def __init__(self, fetch, default_ttl=TOKEN_DEFAULT_TTL,
                 skew=TOKEN_REFRESH_SKEW):
        self._fetch = fetch
        self._default_ttl = default_ttl
        self._skew = skew
        self._token = None
        self._expires_at = 0.0

    def get(self):
        if self._token is None or time.monotonic() >= self._expires_at - self._skew:
            payload = self._fetch()
            self._token = payload["access_token"]
            try:
                ttl = float(payload.get("expires_in") or self._default_ttl)
            except (TypeError, ValueError):
                ttl = self._default_ttl
            self._expires_at = time.monotonic() + ttl
        return self._token

    def invalidate(self):
        """Force the next get() to fetch a fresh token (used after a 401)."""
        self._token = None
        self._expires_at = 0.0


class _PeriodThrottle:
    """Keep each reporting period inside its published query allowance.

    Sliding window per period: remember recent call times, and if the window is
    full, sleep until the oldest call falls out of it.
    """

    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self._calls = {}   # period -> [timestamps]
        self._sleep = sleep
        self._clock = clock

    def wait(self, period):
        max_calls, window = RATE_LIMIT_BY_PERIOD.get(period, DEFAULT_RATE_LIMIT)
        times = self._calls.setdefault(period, [])
        now = self._clock()
        # Drop calls that have aged out of the window.
        cutoff = now - window
        times[:] = [t for t in times if t > cutoff]
        if len(times) >= max_calls:
            wait_for = times[0] + window - now
            if wait_for > 0:
                self._sleep(wait_for)
                now = self._clock()
                cutoff = now - window
                times[:] = [t for t in times if t > cutoff]
        times.append(now)


def build_filter(entity_spec, sub_entities=None, limit=None, skip=None,
                 extra_fields=None, where=None):
    """Build the `filter` JSON object for a request.

    `order` is included whenever skip/limit are used - the spec requires it
    ("ORDER BY required to use Skip/Limit") and paging is otherwise rejected
    or non-deterministic.
    """
    filter_obj = {}

    fields = {}
    for name in (extra_fields or entity_spec.get("extra_fields") or []):
        fields[name] = True
    if fields:
        filter_obj["fields"] = fields

    if where:
        filter_obj["where"] = where

    # One expanded call beats many paged ones against these rate limits.
    available = entity_spec.get("sub_entities") or {}
    wanted = available if sub_entities is None else {
        k: v for k, v in available.items() if k in sub_entities}
    if wanted:
        filter_obj["entities"] = {name: {"include": True} for name in wanted}

    if limit is not None or skip is not None:
        order_by = entity_spec.get("order_by")
        if order_by:
            filter_obj["order"] = {order_by: "ASC"}
        if limit is not None:
            filter_obj["limit"] = limit
        if skip is not None:
            filter_obj["skip"] = skip

    return filter_obj


class ReportingClient:
    """Read-only client for the Reporting API."""

    def __init__(self, client_id, client_secret, retries=2, throttle=None,
                 session=None, sleep=time.sleep):
        self._client_id = client_id
        self._client_secret = client_secret
        self.retries = retries
        self.session = session or requests.Session()
        self._sleep = sleep
        self._throttle = throttle or _PeriodThrottle(sleep=sleep)
        self.tokens = TokenCache(self._fetch_token)
        # Populated with the x-date-range of each call, for the UI log.
        self.last_date_range = ""

    # -- auth ------------------------------------------------------------

    def _fetch_token(self):
        """POST the client credentials for a bearer token.

        Raises ReportingError with no credential material in the message.
        """
        try:
            resp = self.session.post(TOKEN_URL, timeout=REQUEST_TIMEOUT, json={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "audience": TOKEN_AUDIENCE,
                "grant_type": "client_credentials",
            })
        except requests.RequestException as exc:
            raise ReportingError(f"Could not reach the token endpoint: {exc}")
        if resp.status_code in (401, 403):
            raise ReportingError(
                "Authentication failed - check the Client ID and Client "
                "Secret. These come from Settings > Integration Tokens > "
                "'Add client credential' (not 'Add access token').")
        if not resp.ok:
            raise ReportingError(
                f"Token request failed: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise ReportingError("Token endpoint returned a malformed response.")
        if not payload.get("access_token"):
            raise ReportingError("Token endpoint returned no access_token.")
        return payload

    def test_connection(self):
        """Validate credentials cheaply. Returns (ok, message)."""
        try:
            self.tokens.get()
        except ReportingError as exc:
            return False, str(exc)
        rows, error = self._get(ROLES_ENDPOINT, {}, period=None)
        if error:
            return False, error
        return True, f"Connected. {len(rows)} role(s) visible."

    # -- requests --------------------------------------------------------

    def _get(self, endpoint, params, period):
        """GET an endpoint with throttling, token refresh and backoff.

        Returns (rows, error). rows is [] when error is set. Also records the
        response's x-bookmark / x-date-range on self.
        """
        self.last_bookmark = None
        url = f"{ANALYTICS_BASE}{endpoint}"
        refreshed = False

        for attempt in range(self.retries + 1):
            if period:
                self._throttle.wait(period)
            try:
                token = self.tokens.get()
            except ReportingError as exc:
                return [], str(exc)

            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                if attempt < self.retries:
                    self._sleep(2)
                    continue
                return [], f"Connection error: {exc}"

            # Token may have expired: refresh once and retry.
            if resp.status_code == 401 and not refreshed:
                self.tokens.invalidate()
                refreshed = True
                continue

            # Throttled. Status codes are undocumented (the spec declares only
            # 200), so handle both and honour Retry-After when present.
            if resp.status_code in (429, 503):
                if attempt < self.retries:
                    self._sleep(_retry_after_seconds(resp, default=30))
                    continue
                return [], ("Rate limit hit. The Reporting API allows only "
                            f"{_limit_text(period)} - wait and retry, or use a "
                            "shorter reporting period.")

            if not resp.ok:
                return [], f"HTTP {resp.status_code}: {resp.text[:200]}"

            self.last_date_range = resp.headers.get("x-date-range", "") or ""
            self.last_bookmark = resp.headers.get("x-bookmark")
            try:
                body = resp.json()
            except ValueError:
                return [], "Malformed JSON in the response."
            return _rows_from(body), ""

        return [], "Request failed after retries."

    def fetch(self, entity, reporting_period=None, bookmark=None,
              sub_entities=None, limit=None):
        """Fetch one entity. Returns (rows, new_bookmark, date_range, error).

        Sub-entities are expanded in the same call by default (cheaper than
        paging against these rate limits).
        """
        spec = REPORTING_ENTITIES.get(entity)
        if spec is None:
            return [], None, "", f"Unknown reporting entity: {entity!r}"

        filter_obj = build_filter(spec, sub_entities=sub_entities, limit=limit)
        params = {"filter": json.dumps(filter_obj)}
        period = reporting_period if spec.get("has_period") else None
        if period:
            params["reportingPeriod"] = period
        if bookmark and spec.get("bookmarkable"):
            params["bookmark"] = bookmark

        rows, error = self._get(spec["endpoint"], params, period)
        return rows, getattr(self, "last_bookmark", None), self.last_date_range, error

    def fetch_filterable_fields(self, kind):
        """Discover an instance's filterable customer/product custom fields."""
        endpoint = FILTERABLE_FIELDS_ENDPOINTS.get(kind)
        if endpoint is None:
            return [], f"Unknown filterable-field kind: {kind!r}"
        return self._get(endpoint, {"filter": "{}"}, period=None)


def _rows_from(body):
    """Normalise a response body into a list of row dicts.

    The Reporting API's response envelope is not documented consistently, so
    accept a bare list, or a wrapper keyed data/items/results.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("data", "items", "results", "value"):
            value = body.get(key)
            if isinstance(value, list):
                return value
        return [body]
    return []


def _retry_after_seconds(resp, default=30):
    raw = resp.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def _limit_text(period):
    max_calls, window = RATE_LIMIT_BY_PERIOD.get(period, DEFAULT_RATE_LIMIT)
    return f"{max_calls} queries per {window}s for '{period}'"
