"""Offline tests for SkynamoClient - the module that actually writes to the
Public API, and the one that had no coverage at all until a live run failed.

Every request is intercepted by a fake session, so this asserts the exact
bodies and URLs we put on the wire without any network or credentials. The bug
these exist to prevent: the engine tests use a fake client that accepts
whatever it is handed and always says OK, so a wrong request body passes every
one of them and only fails against a real instance.
"""

import base64
import hashlib
import json

import requests

from skynamo_geo.client import SkynamoClient, content_hash_b64
from skynamo_geo.config import API_BASE, FILE_HASH_ALGORITHM


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """Records every call; returns queued responses (or a default 200)."""

    def __init__(self, responses=None, raise_on=None):
        self.headers = {}
        self.calls = []            # (method, url, kwargs)
        self.responses = list(responses or [])
        self.raise_on = raise_on or set()

    def _record(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method in self.raise_on:
            raise requests.RequestException("network down")
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse(200, {"data": [{"id": "guid-1"}]})

    def get(self, url, **kwargs):
        return self._record("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._record("post", url, **kwargs)

    def patch(self, url, **kwargs):
        return self._record("patch", url, **kwargs)


def make_client(session):
    client = SkynamoClient("acme", "secret-key")
    client.session = session
    return client


def body_of(call):
    return call[2]["json"]


# ---------------------------------------------------------------------------
# attach_files - the product patch that failed against a live instance
# ---------------------------------------------------------------------------

# Identified by id when known. ProductPatch declares id required, and patching
# by code alone was rejected live while the id-keyed customer patch succeeded.
s = FakeSession()
ok, err = make_client(s).attach_files("ABC", ["g1", "g2"], product_id=7)
assert ok and err == "", (ok, err)
method, url, _kw = s.calls[0]
assert method == "patch"
assert url == f"{API_BASE}/products", url      # collection endpoint, not /{id}
sent = body_of(s.calls[0])
assert isinstance(sent, list) and len(sent) == 1, sent
assert sent[0]["id"] == 7, sent
assert sent[0]["files"] == ["g1", "g2"], sent
assert "code" not in sent[0], "id alone keys the patch, mirroring update_location"

# Falls back to code only when the product has no id at all.
s = FakeSession()
make_client(s).attach_files("A/B", ["g1"])
sent = body_of(s.calls[0])
assert sent == [{"code": "A/B", "files": ["g1"]}], sent

# id=0 is a real id, not "missing" - a falsy check here would silently fall back.
s = FakeSession()
make_client(s).attach_files("ZERO", [], product_id=0)
assert body_of(s.calls[0])[0]["id"] == 0, body_of(s.calls[0])

# GUIDs go out as strings: Product.files items are declared strings while
# File.id (what POST /files returns) is declared an integer.
s = FakeSession()
make_client(s).attach_files("ABC", [11, "g2", 12], product_id=1)
assert body_of(s.calls[0])[0]["files"] == ["11", "g2", "12"], body_of(s.calls[0])

# An empty list is sent as-is: that is how an image is detached.
s = FakeSession()
make_client(s).attach_files("ABC", [], product_id=1)
assert body_of(s.calls[0])[0]["files"] == []

# A rejection reports the status, the body, and which key we patched by.
s = FakeSession([FakeResponse(400, None, text="id is required")])
ok, err = make_client(s).attach_files("ABC", ["g1"], product_id=9)
assert ok is False
assert "400" in err and "id is required" in err and "patched by id" in err, err

s = FakeSession([FakeResponse(400, None, text="nope")])
ok, err = make_client(s).attach_files("ABC", ["g1"])
assert "patched by code" in err, err

# A network error is returned, not raised - attach_files was the only writer
# without this guard, so a blip aborted the whole run instead of one product.
s = FakeSession(raise_on={"patch"})
ok, err = make_client(s).attach_files("ABC", ["g1"], product_id=1)
assert ok is False and "Connection error" in err, err

# ---------------------------------------------------------------------------
# update_location - the write path that already worked; pin its shape
# ---------------------------------------------------------------------------
s = FakeSession()
ok, err = make_client(s).update_location(42, -33.9, 18.4, accuracy=1000,
                                         is_approximate=True)
assert ok and err == ""
method, url, _kw = s.calls[0]
assert method == "patch" and url == f"{API_BASE}/customers", url
sent = body_of(s.calls[0])
assert sent[0]["id"] == 42
assert sent[0]["location"] == {"latitude": -33.9, "longitude": 18.4,
                               "accuracy": 1000, "is_approximate": True}, sent

s = FakeSession([FakeResponse(403, None, text="denied")])
ok, err = make_client(s).update_location(1, 0.0, 0.0)
assert ok is False and "403" in err and "denied" in err

# ---------------------------------------------------------------------------
# upload_file - POST /files, and the GUID we read back out
# ---------------------------------------------------------------------------
s = FakeSession([FakeResponse(200, {"data": [{"id": "guid-abc"}]})])
guid, err = make_client(s).upload_file("ABC.png", "Ym9keQ==")
assert guid == "guid-abc" and err == "", (guid, err)
method, url, _kw = s.calls[0]
assert method == "post" and url == f"{API_BASE}/files", url
# content_hash is REQUIRED even though FilePost does not list it as required:
# without it a live instance answers "F002: Content hash is required." and
# every single upload fails.
sent = body_of(s.calls[0])
assert set(sent) == {"filename", "content", "content_hash"}, sent
assert sent["filename"] == "ABC.png" and sent["content"] == "Ym9keQ=="
expected = base64.b64encode(
    hashlib.new(FILE_HASH_ALGORITHM, b"body").digest()).decode("ascii")
assert sent["content_hash"] == expected, (sent["content_hash"], expected)
# ...and it is the hash of the decoded bytes, not of the base64 text
assert sent["content_hash"] != base64.b64encode(
    hashlib.new(FILE_HASH_ALGORITHM, b"Ym9keQ==").digest()).decode("ascii")

# A caller that already has the bytes can pass the hash in, and it is used
# verbatim rather than recomputed.
s = FakeSession([FakeResponse(200, {"data": [{"id": "g"}]})])
make_client(s).upload_file("ABC.png", "Ym9keQ==", content_hash="PRECOMPUTED")
assert body_of(s.calls[0])["content_hash"] == "PRECOMPUTED"

# The helper is base64 of the digest of the raw bytes.
assert content_hash_b64(b"body") == expected
assert content_hash_b64(b"") == base64.b64encode(
    hashlib.new(FILE_HASH_ALGORITHM, b"").digest()).decode("ascii")

# Failure modes all report rather than raise.
s = FakeSession([FakeResponse(413, None, text="too large")])
guid, err = make_client(s).upload_file("big.png", "Ym9keQ==")
assert guid is None and "413" in err and "too large" in err

s = FakeSession([FakeResponse(200, None, text="<html>not json</html>")])
guid, err = make_client(s).upload_file("ABC.png", "Ym9keQ==")
assert guid is None and "Malformed response" in err, err

s = FakeSession([FakeResponse(200, {"data": []})])
guid, err = make_client(s).upload_file("ABC.png", "Ym9keQ==")
assert guid is None and "No file GUID" in err, err

s = FakeSession(raise_on={"post"})
guid, err = make_client(s).upload_file("ABC.png", "Ym9keQ==")
assert guid is None and "Connection error" in err, err

# Content that is not valid base64 is reported, never raised: hashing it means
# decoding it, and an exception here would abort the run instead of one image.
s = FakeSession()
guid, err = make_client(s).upload_file("ABC.png", "x")
assert guid is None and "Could not hash the file content" in err, err
assert s.calls == [], "nothing should be sent if the content cannot be hashed"

# A caller-supplied hash skips the decode entirely, so the same content is fine.
s = FakeSession([FakeResponse(200, {"data": [{"id": "g"}]})])
guid, err = make_client(s).upload_file("ABC.png", "x", content_hash="H")
assert guid == "g" and err == "", (guid, err)

# ---------------------------------------------------------------------------
# test_connection - auth failures are named as such
# ---------------------------------------------------------------------------
s = FakeSession([FakeResponse(401, None, text="nope")])
ok, msg = make_client(s).test_connection()
assert ok is False and "Authentication failed" in msg, msg

s = FakeSession([FakeResponse(200, {"data": []})])
ok, msg = make_client(s).test_connection()
assert ok is True and msg == "Connected."

# Credentials go in headers, and are not in any URL or body.
client = SkynamoClient("acme", "secret-key")
assert client.session.headers["X-API-CLIENT"] == "acme"
assert client.session.headers["X-API-KEY"] == "secret-key"

print("All client tests passed")
