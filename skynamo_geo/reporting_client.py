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
    RATE_LIMIT_BY_PERIOD, REPORTING_ENTITIES, REPORTING_NETWORK_RETRIES,
    REPORTING_TIMEOUT, ROLES_ENDPOINT,
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

        Raises ReportingError. The message includes the server's own OAuth
        error fields when present - those name the actual problem (e.g.
        invalid_client vs access_denied vs an audience mismatch) and are not
        credential material. The client id/secret are never included.
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

        # Parsed only on the failure paths - the happy path parses once, below.
        if resp.status_code in (401, 403):
            detail = _oauth_error_detail(resp)
            raise ReportingError(
                "Authentication failed"
                + (f" ({detail})" if detail else "")
                + ". Check the Client ID and Client Secret: they come from "
                  "Skynamo insights > Settings > Integration Tokens > "
                  "'Add client credential', NOT 'Add access token' (that "
                  "button issues an x-api-key for the Public API instead). "
                  "If the 'Add client credential' button is missing, the "
                  "Reporting API add-on is probably not enabled on this "
                  "subscription.")
        if not resp.ok:
            detail = _oauth_error_detail(resp)
            raise ReportingError(
                f"Token request failed: HTTP {resp.status_code}"
                + (f" - {detail}" if detail else ""))
        try:
            payload = resp.json()
        except ValueError:
            raise ReportingError("Token endpoint returned a malformed response.")
        if not payload.get("access_token"):
            detail = _oauth_error_detail(resp)
            raise ReportingError(
                "Token endpoint returned no access_token"
                + (f" - {detail}" if detail else ""))
        return payload

    def test_connection(self):
        """Validate credentials. Returns (ok, message).

        A token alone is not proof of access: the audience can be wrong, or the
        credential can exist without being entitled to the Reporting add-on. In
        both cases a token is issued and every data call then 401s. So an
        auth-class failure on the probe is a genuine failure.

        Anything else from the probe is only informational - /v2/roles is one of
        the endpoints the spec leaves undocumented, so a quirk there must not
        report working credentials as broken.
        """
        try:
            self.tokens.get()
        except ReportingError as exc:
            return False, str(exc)

        # Documented minimum filter is an empty JSON object.
        rows, error, status = self._get(ROLES_ENDPOINT, {"filter": "{}"},
                                        period=None)
        if error and status in (401, 403):
            return False, (
                f"A token was issued, but the API rejected it ({error}). That "
                "is an audience or entitlement problem rather than a wrong "
                "secret: the credential exists but is not permitted to read "
                "the Reporting API. Ask Skynamo support to confirm the "
                "Reporting/Analytics add-on is enabled for this credential.")
        if error:
            return True, ("Credentials OK (token issued), but the /v2/roles "
                          f"probe failed: {error}. This may just be an "
                          "endpoint quirk - try an extract.")
        return True, f"Connected. {len(rows)} role(s) visible."

    # -- requests --------------------------------------------------------

    def _get(self, endpoint, params, period):
        """GET an endpoint with throttling, token refresh and backoff.

        Returns (rows, error, status). rows is [] when error is set; status is
        the HTTP status code when one was received, else None - callers need it
        to tell an auth rejection from an endpoint quirk.
        """
        self.last_bookmark = None
        url = f"{ANALYTICS_BASE}{endpoint}"
        refreshed = False
        network_failures = 0

        for attempt in range(self.retries + 1):
            if period:
                self._throttle.wait(period)
            try:
                token = self.tokens.get()
            except ReportingError as exc:
                return [], str(exc), None

            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = self.session.get(url, params=params, headers=headers,
                                        timeout=REPORTING_TIMEOUT)
            except requests.RequestException as exc:
                # Every retry re-enters the throttle above, so on a tight
                # period each one costs a slot of the published allowance.
                # Retry network failures at most REPORTING_NETWORK_RETRIES
                # times rather than burning the whole budget.
                network_failures += 1
                if network_failures <= REPORTING_NETWORK_RETRIES:
                    self._sleep(2)
                    continue
                hint = ""
                if isinstance(exc, requests.Timeout):
                    hint = (f" The request did not complete within "
                            f"{REPORTING_TIMEOUT}s - this endpoint returns "
                            f"every expanded sub-entity in one response, so a "
                            f"wide reporting period can be very large. Try a "
                            f"shorter period.")
                return [], f"Connection error: {exc}.{hint}", None

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
                            "shorter reporting period."), resp.status_code

            if not resp.ok:
                return ([], f"HTTP {resp.status_code}: {resp.text[:200]}",
                        resp.status_code)

            self.last_date_range = resp.headers.get("x-date-range", "") or ""
            self.last_bookmark = resp.headers.get("x-bookmark")
            try:
                body = resp.json()
            except ValueError:
                return [], "Malformed JSON in the response.", resp.status_code
            return _rows_from(body), "", resp.status_code

        return [], "Request failed after retries.", None

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

        rows, error, _status = self._get(spec["endpoint"], params, period)
        return rows, getattr(self, "last_bookmark", None), self.last_date_range, error

    def fetch_filterable_fields(self, kind):
        """Discover an instance's filterable customer/product custom fields.

        Returns (rows, error).
        """
        endpoint = FILTERABLE_FIELDS_ENDPOINTS.get(kind)
        if endpoint is None:
            return [], f"Unknown filterable-field kind: {kind!r}"
        rows, error, _status = self._get(endpoint, {"filter": "{}"},
                                         period=None)
        return rows, error


def _oauth_error_detail(resp):
    """Pull the server's OAuth error fields out of a failed token response.

    Returns something like "invalid_client: Client authentication failed", or
    "" when there is nothing useful.

    ONLY the two standard OAuth error fields are read. Deliberately not the raw
    body and not a generic `message` field: a gateway in front of the token
    endpoint can reject a request by echoing it back, and the request body
    contains the client_id and client_secret. Splicing that into an exception
    would put credentials into the GUI log and any saved report.
    """
    try:
        body = resp.json()
    except (ValueError, AttributeError):
        return ""
    if not isinstance(body, dict):
        return ""
    code = str(body.get("error") or "").strip()
    description = str(body.get("error_description") or "").strip()
    if code and description:
        return f"{code}: {description}"[:200]
    return (code or description)[:200]


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
