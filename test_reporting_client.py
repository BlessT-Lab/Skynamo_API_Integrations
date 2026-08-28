"""Offline tests for the Reporting API client: token caching/refresh, 401
retry, 429 backoff, per-period throttling, filter building, and that
credentials never leak into messages. Uses a fake session - no network."""

import json

from skynamo_geo import reporting_client
from skynamo_geo.reporting_client import (
    ReportingClient, ReportingError, TokenCache, _PeriodThrottle, build_filter,
    _rows_from,
)
from skynamo_geo.reporting_config import REPORTING_ENTITIES

SECRET = "super-secret-value"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=""):
        self.status_code = status
        self._body = body if body is not None else []
        self.headers = headers or {}
        self.text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is _MALFORMED:
            raise ValueError("not json")
        return self._body


_MALFORMED = object()


class FakeSession:
    """Scripted session: queue up responses, record the requests made."""

    def __init__(self, token_responses=None, get_responses=None):
        self.token_responses = list(token_responses or [])
        self.get_responses = list(get_responses or [])
        self.posts = []
        self.gets = []

    def post(self, url, timeout=None, json=None):
        self.posts.append({"url": url, "json": json})
        if self.token_responses:
            return self.token_responses.pop(0)
        return FakeResponse(200, {"access_token": "tok", "expires_in": 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append({"url": url, "params": params, "headers": headers})
        if self.get_responses:
            return self.get_responses.pop(0)
        return FakeResponse(200, [])


# --- TokenCache: caches, then refreshes when expired ---
calls = {"n": 0}
def fetch_token():
    calls["n"] += 1
    return {"access_token": f"tok-{calls['n']}", "expires_in": 3600}

cache = TokenCache(fetch_token)
assert cache.get() == "tok-1"
assert cache.get() == "tok-1"          # cached, not refetched
assert calls["n"] == 1
cache.invalidate()
assert cache.get() == "tok-2"          # refetched after invalidate
assert calls["n"] == 2

# A short/absent expires_in still yields a usable token
short = TokenCache(lambda: {"access_token": "t", "expires_in": None},
                  default_ttl=600)
assert short.get() == "t"
# Expiry honoured: ttl smaller than the skew forces a refetch every call
n2 = {"n": 0}
def tiny():
    n2["n"] += 1
    return {"access_token": "x", "expires_in": 1}
expiring = TokenCache(tiny, skew=60)
expiring.get(); expiring.get()
assert n2["n"] == 2, "a token inside the skew window must be refreshed"

# --- build_filter ---
act = REPORTING_ENTITIES["activities"]
f = build_filter(act)
# Every sub-entity the registry declares is expanded by default - they come
# back in the same call, so this costs no extra requests.
assert set(f["entities"]) == set(act["sub_entities"])
assert all(v == {"include": True} for v in f["entities"].values())
assert "limit" not in f and "order" not in f
# the breakdown the whole point of this: each document type is requested
for expected in ("orderTotals", "orders", "quoteTotals", "quotes",
                 "creditRequestTotals", "creditRequests", "visits",
                 "surveys", "forms", "comments", "emails"):
    assert expected in f["entities"], expected

# order is mandatory whenever skip/limit are used
f2 = build_filter(act, limit=100, skip=0)
assert f2["limit"] == 100 and f2["skip"] == 0
assert f2["order"] == {"start_time": "ASC"}, f2.get("order")

# Fields that are off by default get requested explicitly
cust = REPORTING_ENTITIES["customers"]
f3 = build_filter(cust)
assert f3["fields"]["display_name"] is True
assert f3["fields"]["added_by_user_id"] is True

# Selecting a subset of sub-entities
f4 = build_filter(act, sub_entities=["visits"])
assert set(f4["entities"]) == {"visits"}

# An entity with no sub-entities gets no `entities` key
f5 = build_filter(REPORTING_ENTITIES["products"])
assert "entities" not in f5

# An entity with no sortable field documented gets no order (so no paging)
f6 = build_filter(REPORTING_ENTITIES["invoices"], limit=10)
assert "order" not in f6 and f6["limit"] == 10

# --- _rows_from: tolerate the undocumented envelope shapes ---
assert _rows_from([{"a": 1}]) == [{"a": 1}]
assert _rows_from({"data": [{"a": 1}]}) == [{"a": 1}]
assert _rows_from({"items": [{"b": 2}]}) == [{"b": 2}]
assert _rows_from({"a": 1}) == [{"a": 1}]
assert _rows_from(None) == []

# --- fetch: params carry filter/period/bookmark correctly ---
session = FakeSession(get_responses=[
    FakeResponse(200, [{"activity_id": "a1"}],
                 {"x-bookmark": "884", "x-date-range": "2026-07-01..2026-07-31"}),
])
client = ReportingClient("id", SECRET, session=session, sleep=lambda s: None)
rows, bookmark, date_range, error = client.fetch(
    "activities", reporting_period="ThisMonth")
assert error == "" and rows == [{"activity_id": "a1"}]
assert bookmark == "884"
assert date_range == "2026-07-01..2026-07-31"
sent = session.gets[0]["params"]
assert sent["reportingPeriod"] == "ThisMonth"
assert "bookmark" not in sent                     # none known yet
assert json.loads(sent["filter"])["entities"]     # expanded
assert session.gets[0]["headers"]["Authorization"].startswith("Bearer ")

# bookmark is sent when supplied
session2 = FakeSession(get_responses=[FakeResponse(200, [])])
c2 = ReportingClient("id", SECRET, session=session2, sleep=lambda s: None)
c2.fetch("activities", reporting_period="ThisMonth", bookmark="884")
assert session2.gets[0]["params"]["bookmark"] == "884"

# an endpoint with no reporting period must not receive one
session3 = FakeSession(get_responses=[FakeResponse(200, [])])
c3 = ReportingClient("id", SECRET, session=session3, sleep=lambda s: None)
c3.fetch("products", reporting_period="ThisMonth")
assert "reportingPeriod" not in session3.gets[0]["params"]

# unknown entity is reported, not raised
rows, _bm, _dr, err = c3.fetch("nope")
assert rows == [] and "Unknown reporting entity" in err

# --- 401 triggers exactly one refresh-and-retry ---
session4 = FakeSession(
    token_responses=[
        FakeResponse(200, {"access_token": "t1", "expires_in": 3600}),
        FakeResponse(200, {"access_token": "t2", "expires_in": 3600}),
    ],
    get_responses=[FakeResponse(401), FakeResponse(200, [{"ok": 1}])])
c4 = ReportingClient("id", SECRET, session=session4, sleep=lambda s: None)
rows, _bm, _dr, err = c4.fetch("products")
assert err == "" and rows == [{"ok": 1}]
assert len(session4.posts) == 2, "should have fetched a second token"
assert session4.gets[1]["headers"]["Authorization"] == "Bearer t2"

# --- 429 backs off, honours Retry-After, then reports the limit ---
slept = []
session5 = FakeSession(get_responses=[
    FakeResponse(429, headers={"Retry-After": "7"}),
    FakeResponse(429, headers={"Retry-After": "7"}),
    FakeResponse(429, headers={"Retry-After": "7"}),
])
c5 = ReportingClient("id", SECRET, session=session5, sleep=slept.append)
rows, _bm, _dr, err = c5.fetch("activities", reporting_period="AllData")
assert rows == [] and "Rate limit" in err
assert "2 queries per 600s" in err, err          # AllData's published tier
assert 7 in slept, slept                          # Retry-After honoured

# 503 is treated like 429 (throttle status codes are undocumented)
session6 = FakeSession(get_responses=[FakeResponse(503), FakeResponse(200, [])])
c6 = ReportingClient("id", SECRET, session=session6, sleep=lambda s: None)
_rows, _bm, _dr, err = c6.fetch("products")
assert err == ""

# --- bad credentials: clear message, and the secret never appears ---
session7 = FakeSession(token_responses=[FakeResponse(401)])
c7 = ReportingClient("id", SECRET, session=session7, sleep=lambda s: None)
ok, message = c7.test_connection()
assert ok is False
assert "Add client credential" in message      # points at the right button
assert SECRET not in message

# The raised error must not carry credential material either (fresh client, so
# the queued 401 is the one that answers).
session7b = FakeSession(token_responses=[FakeResponse(401)])
c7b = ReportingClient("id", SECRET, session=session7b, sleep=lambda s: None)
try:
    c7b.tokens.get()
    raise AssertionError("expected ReportingError")
except ReportingError as exc:
    assert SECRET not in str(exc), "secret must never appear in an error"

# malformed token payload
session8 = FakeSession(token_responses=[FakeResponse(200, {"nope": 1})])
c8 = ReportingClient("id", SECRET, session=session8, sleep=lambda s: None)
ok, message = c8.test_connection()
assert ok is False and "access_token" in message

# --- throttle: spacing matches the period's published tier ---
now = {"t": 0.0}
naps = []
def clock():
    return now["t"]
def sleeper(seconds):
    naps.append(seconds)
    now["t"] += seconds

throttle = _PeriodThrottle(sleep=sleeper, clock=clock)
# AllData allows 2 per 600s: the third call must wait.
throttle.wait("AllData")
throttle.wait("AllData")
assert naps == []
throttle.wait("AllData")
assert naps and naps[0] > 0, "third AllData call must be throttled"

# A generous tier does not sleep for a handful of calls.
naps.clear()
for _ in range(5):
    throttle.wait("Prev30Days")          # 30 per 30s
assert naps == []

# Different periods have independent budgets.
naps.clear()
t2 = _PeriodThrottle(sleep=sleeper, clock=clock)
t2.wait("AllData"); t2.wait("AllData")
t2.wait("ThisDay")                        # different period, no wait
assert naps == []

# An unknown period falls back to the most conservative budget.
naps.clear()
t3 = _PeriodThrottle(sleep=sleeper, clock=clock)
t3.wait("MadeUpPeriod"); t3.wait("MadeUpPeriod"); t3.wait("MadeUpPeriod")
assert naps and naps[0] > 0

# --- test_connection happy path ---
session9 = FakeSession(get_responses=[FakeResponse(200, [{"roleId": 1}])])
c9 = ReportingClient("id", SECRET, session=session9, sleep=lambda s: None)
ok, message = c9.test_connection()
assert ok is True and "1 role" in message
# the probe sends the documented minimum filter
assert session9.gets[0]["params"] == {"filter": "{}"}

# --- a token that the API then REJECTS must not report as connected -------
# Regression: a wrong audience or an unentitled credential still yields a
# token, then every data call 401s. Reporting that as "Connected" sends the
# user off to run extracts that cannot possibly work.
for bad_status in (401, 403):
    # two 401s: the client refreshes once, then gives up
    session = FakeSession(get_responses=[FakeResponse(bad_status),
                                        FakeResponse(bad_status),
                                        FakeResponse(bad_status)])
    client = ReportingClient("id", SECRET, session=session,
                             sleep=lambda s: None)
    ok, message = client.test_connection()
    assert ok is False, f"{bad_status} probe must fail the connection"
    assert "audience or entitlement" in message, message
    assert "add-on" in message

# ...but a NON-auth probe failure is only a caveat, since /v2/roles is one of
# the undocumented endpoints and may simply be quirky.
session_quirk = FakeSession(get_responses=[FakeResponse(400, text="odd")])
c_quirk = ReportingClient("id", SECRET, session=session_quirk,
                          sleep=lambda s: None)
ok, message = c_quirk.test_connection()
assert ok is True, "a 400 from the probe should not block the user"
assert "Credentials OK" in message and "quirk" in message

# --- _get returns the status code so callers can tell those cases apart ---
session_st = FakeSession(get_responses=[FakeResponse(200, [{"a": 1}])])
c_st = ReportingClient("id", SECRET, session=session_st, sleep=lambda s: None)
rows, error, status = c_st._get("/v2/roles", {}, period=None)
assert rows == [{"a": 1}] and error == "" and status == 200
session_st2 = FakeSession(get_responses=[FakeResponse(500, text="boom")])
c_st2 = ReportingClient("id", SECRET, session=session_st2, sleep=lambda s: None)
rows, error, status = c_st2._get("/v2/roles", {}, period=None)
assert rows == [] and status == 500 and "500" in error

# fetch_filterable_fields still returns a 2-tuple for its callers
session_ff = FakeSession(get_responses=[FakeResponse(200, [{"fieldId": 1}])])
c_ff = ReportingClient("id", SECRET, session=session_ff, sleep=lambda s: None)
rows, error = c_ff.fetch_filterable_fields("customer")
assert rows == [{"fieldId": 1}] and error == ""
rows, error = c_ff.fetch_filterable_fields("nonsense")
assert rows == [] and "Unknown filterable-field kind" in error

# --- token errors surface the server's OAuth detail, never the request ----
session_detail = FakeSession(token_responses=[FakeResponse(
    401, {"error": "invalid_client",
          "error_description": "Client authentication failed"})])
c_detail = ReportingClient("id", SECRET, session=session_detail,
                           sleep=lambda s: None)
ok, message = c_detail.test_connection()
assert ok is False
assert "invalid_client" in message and "Client authentication failed" in message
assert SECRET not in message

# A gateway that rejects by echoing the request must NOT have that echoed on:
# the body would contain client_id and client_secret.
echoed = {"message": f'Invalid request body: {{"client_secret":"{SECRET}"}}'}
session_echo = FakeSession(token_responses=[FakeResponse(400, echoed)])
c_echo = ReportingClient("id", SECRET, session=session_echo,
                         sleep=lambda s: None)
ok, message = c_echo.test_connection()
assert ok is False
assert SECRET not in message, "must not splice an echoed request into the error"
# same for a non-JSON echo
session_echo2 = FakeSession(token_responses=[
    FakeResponse(400, _MALFORMED, text=f'client_secret={SECRET}')])
c_echo2 = ReportingClient("id", SECRET, session=session_echo2,
                          sleep=lambda s: None)
ok, message = c_echo2.test_connection()
assert ok is False and SECRET not in message

print("All reporting client tests passed")
