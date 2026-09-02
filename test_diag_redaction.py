"""Offline tests that the diagnostics never print a secret.

Both scripts tell the user their output can be shared, so that claim has to be
enforced by a test rather than by a docstring. Every value below is a canary:
if any of them reaches stdout, the test fails.

Covers diag_reporting_auth.py (OAuth tokens, client id/secret, JWT claims) and
diag_image_upload.py (the Public API key).
"""

import io
import json
from contextlib import redirect_stdout

import diag_image_upload as imgdiag
import diag_reporting_auth as diag

TOKEN = "eyJhbGciOiJSUzI1NiJ9.SUPERSECRETTOKENPAYLOAD.sig"
REFRESH = "REFRESHCANARY123456"
CLIENT_ID = "abcdef01-2345-6789-abcd-ef0123456789"
SECRET = "SuperSecretClientValue0123456789"


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, text=None,
                 reason="OK"):
        self.status_code = status
        self.reason = reason
        self._body = body
        self.headers = headers or {}
        self._text = text

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    @property
    def text(self):
        if self._text is not None:
            return self._text
        return "" if self._body is None else json.dumps(self._body)

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


def captured(fn, *args, **kwargs):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kwargs)
    return buf.getvalue()


# --- fingerprint: identifies without revealing ---
fp = diag.fingerprint(SECRET)
assert str(len(SECRET)) in fp
assert SECRET not in fp
assert SECRET[:3] not in fp, "must not leak a prefix of the secret"
assert SECRET[-2:] not in fp, "must not leak a suffix of the secret"
# same input -> same fingerprint (so two entries can be compared)
assert diag.fingerprint(SECRET) == fp
assert diag.fingerprint(SECRET + "x") != fp
# a short value cannot be reconstructed either
short = "abcd"
assert short not in diag.fingerprint(short)

# --- redact: strips token-bearing keys at any depth ---
red = diag.redact({
    "access_token": TOKEN,
    "refresh_token": REFRESH,
    "id_token": TOKEN,
    "expires_in": 86400,
    "nested": [{"access_token": TOKEN}],
})
blob = json.dumps(red)
assert TOKEN not in blob and REFRESH not in blob
assert "SUPERSECRET" not in blob
assert red["expires_in"] == 86400, "non-secret fields must survive"
assert "redacted" in red["access_token"]
assert "redacted" in red["nested"][0]["access_token"]

# --- show_response on a successful token body: no token on stdout ---
out = captured(diag.show_response, FakeResponse(
    200, {"access_token": TOKEN, "refresh_token": REFRESH,
          "expires_in": 86400, "token_type": "Bearer"}))
assert TOKEN not in out, "the bearer token must never be printed"
assert REFRESH not in out
assert "SUPERSECRET" not in out
assert "86400" in out, "harmless fields should still be visible"

# --- show_response on a non-JSON body: never echoed (it can mirror the request) ---
echoed = ('Invalid request body: {"client_id":"' + CLIENT_ID
          + '","client_secret":"' + SECRET + '"}')
out = captured(diag.show_response, FakeResponse(400, None, text=echoed,
                                                reason="Bad Request"))
assert SECRET not in out and CLIENT_ID not in out, \
    "a non-JSON body may echo the request - it must not be printed"
assert "not printed" in out

# --- rows_as_shape: field names yes, row values no ---
out = captured(diag.show_response, FakeResponse(200, [
    {"customer_id": "c1", "name": "Acme Wholesale Ltd",
     "code": "SECRETCODE", "latitude": -33.9},
]), rows_as_shape=True)
assert "customer_id" in out and "name" in out, "field names are useful"
assert "Acme Wholesale Ltd" not in out, "row values must not be printed"
assert "SECRETCODE" not in out
assert "rows: 1" in out

# wrapped envelope shape works too
out = captured(diag.show_response,
               FakeResponse(200, {"data": [{"customer_id": "c1",
                                            "name": "Acme"}]}),
               rows_as_shape=True)
assert "rows: 1" in out and "Acme" not in out

# empty body is handled
out = captured(diag.show_response, FakeResponse(204, None, text=""))
assert "empty" in out

# --- decode_jwt_claims + the claim filter used by main() ---
import base64
claims = {"iss": "https://login.skynamo.me/", "aud": diag.TOKEN_AUDIENCE,
          "sub": f"{CLIENT_ID}@clients", "azp": CLIENT_ID, "exp": 1}
payload = base64.urlsafe_b64encode(
    json.dumps(claims).encode()).decode().rstrip("=")
decoded = diag.decode_jwt_claims(f"header.{payload}.sig")
assert decoded["azp"] == CLIENT_ID, "decoding itself is fine"
# the identifying claims are the ones main() redacts
assert "sub" in diag._IDENTIFYING_CLAIMS and "azp" in diag._IDENTIFYING_CLAIMS
assert diag.decode_jwt_claims("not-a-jwt") is None
assert diag.decode_jwt_claims("a.!!!.c") is None

# ---------------------------------------------------------------------------
# diag_image_upload.py - the Public API key must never reach stdout
# ---------------------------------------------------------------------------
API_KEY = "SkynamoApiKeyCanary0123456789"


class ImgResponse:
    """A response whose body echoes the request - the case that leaks."""

    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = text
        self.headers = headers or {"content-type": "application/json"}


imgdiag._SECRET["value"] = API_KEY
try:
    # A gateway that rejects by echoing the request back sends the headers,
    # and the API key is one of them.
    echoed = json.dumps({"message": "rejected",
                         "headers": {"X-API-KEY": API_KEY}})
    buf = io.StringIO()
    with redirect_stdout(buf):
        imgdiag.show("POST /files", ImgResponse(400, echoed))
    out = buf.getvalue()
    assert API_KEY not in out, out
    assert "<API-KEY-REDACTED>" in out, out
    assert "400" in out

    # Non-JSON bodies go through the same redaction, not around it.
    buf = io.StringIO()
    with redirect_stdout(buf):
        imgdiag.show("probe", ImgResponse(500, f"<html>{API_KEY}</html>"))
    assert API_KEY not in buf.getvalue()

    # An empty body must not blow up, and a header we surface is fine.
    buf = io.StringIO()
    with redirect_stdout(buf):
        imgdiag.show("probe", ImgResponse(429, "",
                                          {"retry-after": "30"}))
    assert "retry-after: 30" in buf.getvalue()

    assert imgdiag.redact(f"a {API_KEY} b") == "a <API-KEY-REDACTED> b"
    assert imgdiag.redact(None) is None
finally:
    imgdiag._SECRET["value"] = ""

# With no key recorded yet, redact is a passthrough rather than an error.
assert imgdiag.redact("plain text") == "plain text"

# The write probe is opt-in: `ask` must default to NO, so an unattended run
# (or a bare Enter) never writes to the instance.
assert imgdiag.ask.__defaults__ == (False,)

print("All diagnostic redaction tests passed")
