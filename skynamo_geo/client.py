"""Skynamo public API client."""

import base64
import hashlib
import json
import time

import requests

from .config import (
    API_BASE, DEFAULT_ACCURACY, FILE_HASH_ALGORITHM, PAGE_SIZE,
    PUBLIC_API_BACKOFF_SECONDS, PUBLIC_API_MAX_RETRIES, REQUEST_TIMEOUT,
)


def content_hash_b64(raw_bytes, algorithm=FILE_HASH_ALGORITHM):
    """The `content_hash` POST /files wants: base64 of the content's digest.

    The spec calls it "(base64string)" without naming an algorithm; see
    FILE_HASH_ALGORITHM. Omitting the field entirely is rejected with
    "F002: Content hash is required."
    """
    return base64.b64encode(
        hashlib.new(algorithm, raw_bytes).digest()).decode("ascii")


def create_date_filter(since):
    """The `filters` value for "created on or after `since`" (a date).

    GET /scheduledvisits, /visitfrequencies and /tasks filter only on id,
    create_date and row_version - never on due date, user or customer. The
    spec's own examples disagree on whether a space follows the comma
    ("less_than(id,5)" vs "greater_than_equals(create_date, 2026-01-01)"), so
    this sends none; diag_route_planning.py checks both against a live API.
    """
    return json.dumps([f"greater_than_equals(create_date,{since.isoformat()})"])


def _envelope_items(body):
    """The list of objects in a response, whichever shape it arrived in.

    Collection reads answer {data: [...], page: {...}}; a few single-object
    reads answer {data: [obj]}, {data: obj} or the bare object.
    """
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        data = body.get("data", body)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    return []


class SkynamoClient:
    def __init__(self, instance_name, api_key):
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "X-API-CLIENT": instance_name,
            "X-API-KEY": api_key,
        })

    def test_connection(self):
        """Make a minimal call to validate credentials. Returns (ok, message)."""
        try:
            resp = self.session.get(
                f"{API_BASE}/customers",
                params={"page_number": 1, "page_size": 1},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return False, f"Connection error: {exc}"
        if resp.status_code in (401, 403):
            return False, "Authentication failed - check your API key and instance name."
        if not resp.ok:
            return False, f"Unexpected response: HTTP {resp.status_code} - {resp.text[:500]}"
        return True, "Connected."

    def fetch_all_customers(self, on_page=None, active_only=True):
        """Paginate through /customers using the API's paging response.

        on_page(fetched_count, total_or_None) is called after each page so a
        front-end can show progress while loading.

        When active_only is True (default), customers whose top-level `active`
        flag is False are skipped and never returned. Pagination still uses the
        raw page counts so termination is unaffected by filtering.
        """
        customers = []
        raw_count = 0
        page_number = 1
        while True:
            resp = self.session.get(
                f"{API_BASE}/customers",
                params={"page_number": page_number, "page_size": PAGE_SIZE},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            page_items = body.get("data", [])
            if not page_items:
                break
            raw_count += len(page_items)
            if active_only:
                # `active` defaults to True in the API, so treat missing as active.
                page_items = [c for c in page_items if c.get("active", True)]
            customers.extend(page_items)
            total = (body.get("page") or {}).get("total_item_count")
            if on_page:
                on_page(len(customers), total)
            if total and raw_count >= total:
                break
            if len(body["data"]) < PAGE_SIZE:
                break
            page_number += 1
        return customers

    # -- Generic reads (route planning) ------------------------------------
    #
    # These return (value, error) and never raise, unlike the older
    # fetch_all_customers/fetch_all_products: a route plan reads five
    # endpoints, and one failing must be reportable rather than fatal.

    def _get(self, path, params=None, sleep=time.sleep):
        """GET with backoff on 429/503. Returns (body, error, status)."""
        attempt = 0
        while True:
            try:
                resp = self.session.get(f"{API_BASE}{path}", params=params,
                                        timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                return None, f"Connection error: {exc}", None
            if resp.status_code in (429, 503) and attempt < PUBLIC_API_MAX_RETRIES:
                attempt += 1
                try:
                    wait = float(resp.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    wait = PUBLIC_API_BACKOFF_SECONDS * (2 ** (attempt - 1))
                sleep(max(0.0, wait))
                continue
            if not resp.ok:
                return (None, f"HTTP {resp.status_code}: {resp.text[:500]}",
                        resp.status_code)
            try:
                return resp.json(), "", resp.status_code
            except ValueError:
                return None, f"Malformed response from {path}", resp.status_code

    def _fetch_all(self, path, filters=None, on_page=None, should_cancel=None,
                   sleep=time.sleep):
        """Page through a collection. Returns (items, error).

        Stops on an empty page, a short page, or once total_item_count rows
        have been seen. Rows are de-duplicated by id, since a row inserted
        while paging shifts every later page by one. On error or cancel the
        rows read so far are returned alongside the reason.
        """
        items, seen = [], set()
        raw = 0
        page_number = 1
        while True:
            if should_cancel and should_cancel():
                return items, "Cancelled"
            params = {"page_number": page_number, "page_size": PAGE_SIZE}
            if filters:
                params["filters"] = filters
            body, error, _status = self._get(path, params, sleep=sleep)
            if error:
                return items, error
            page_items = _envelope_items(body)
            if not page_items:
                break
            raw += len(page_items)
            for item in page_items:
                key = item.get("id") if isinstance(item, dict) else None
                if key is not None:
                    if key in seen:
                        continue
                    seen.add(key)
                items.append(item)
            total = ((body.get("page") or {}).get("total_item_count")
                     if isinstance(body, dict) else None)
            if on_page:
                on_page(len(items), total)
            if total and raw >= total:
                break
            if len(page_items) < PAGE_SIZE:
                break
            page_number += 1
        return items, ""

    def fetch_users(self, on_page=None, sleep=time.sleep):
        """All users. Returns (users, error). /users takes no filters."""
        return self._fetch_all("/users", on_page=on_page, sleep=sleep)

    def fetch_visit_frequencies(self, on_page=None, should_cancel=None,
                                sleep=time.sleep):
        """All visit-frequency records. Returns (records, error).

        Each is {customer_id, user_id, frequency, cycle, period}: "frequency
        visits every cycle periods", e.g. 1 / 2 / week = once every 2 weeks.
        """
        return self._fetch_all("/visitfrequencies", on_page=on_page,
                               should_cancel=should_cancel, sleep=sleep)

    def fetch_scheduled_visits(self, created_since=None, on_page=None,
                               should_cancel=None, sleep=time.sleep):
        """Scheduled visits, optionally only those created since a date.

        Returns (visits, error). The API cannot filter by due date, rep or
        customer, so the caller filters those locally.
        """
        filters = create_date_filter(created_since) if created_since else None
        return self._fetch_all("/scheduledvisits", filters=filters,
                               on_page=on_page, should_cancel=should_cancel,
                               sleep=sleep)

    def fetch_interactions_page(self, page_number, sleep=time.sleep):
        """One page of /interactions. Returns (items, page_info, error).

        GET /interactions accepts no filter whatsoever, so the last visit per
        customer can only be found by walking pages. The engine decides which
        pages and in what order; this only fetches one.
        """
        body, error, _status = self._get(
            "/interactions",
            {"page_number": page_number, "page_size": PAGE_SIZE}, sleep=sleep)
        if error:
            return [], {}, error
        page = body.get("page") if isinstance(body, dict) else None
        return _envelope_items(body), page or {}, ""

    def get_configuration(self, sleep=time.sleep):
        """The instance configuration (time zone, distance unit, ...).

        Returns (config_dict, error). Accepts a bare object or an envelope.
        """
        body, error, _status = self._get("/configurations", sleep=sleep)
        if error:
            return {}, error
        items = _envelope_items(body)
        return (items[0] if items else {}), ""

    def get_scheduled_visit(self, visit_id, sleep=time.sleep):
        """One scheduled visit. Returns (visit_or_None, error, status).

        The status lets a caller tell "gone" (404) from "could not ask".
        """
        body, error, status = self._get(f"/scheduledvisits/{visit_id}",
                                        sleep=sleep)
        if error:
            return None, error, status
        items = _envelope_items(body)
        return (items[0] if items else None), "", status

    def update_location(self, customer_id, latitude, longitude,
                        accuracy=DEFAULT_ACCURACY, is_approximate=False):
        """PATCH a customer's location. Returns (ok, error_message).

        The Skynamo API only accepts updates on the collection endpoint
        (PATCH /customers) with an array of CustomerPatch objects;
        /customers/{id} is GET-only.
        """
        resp = self.session.patch(
            f"{API_BASE}/customers",
            json=[{
                "id": customer_id,
                "location": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy": accuracy,
                    "is_approximate": is_approximate,
                },
            }],
            timeout=REQUEST_TIMEOUT,
        )
        if resp.ok:
            return True, ""
        return False, f"HTTP {resp.status_code}: {resp.text[:500]}"

    def fetch_all_products(self, on_page=None, active_only=True):
        """Paginate through /products (same paging shape as /customers).

        on_page(fetched_count, total_or_None) is called after each page.
        When active_only is True, products whose `active` flag is False are
        skipped; pagination still uses the raw page counts so termination is
        unaffected by filtering.
        """
        products = []
        raw_count = 0
        page_number = 1
        while True:
            resp = self.session.get(
                f"{API_BASE}/products",
                params={"page_number": page_number, "page_size": PAGE_SIZE},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            page_items = body.get("data", [])
            if not page_items:
                break
            raw_count += len(page_items)
            if active_only:
                page_items = [p for p in page_items if p.get("active", True)]
            products.extend(page_items)
            total = (body.get("page") or {}).get("total_item_count")
            if on_page:
                on_page(len(products), total)
            if total and raw_count >= total:
                break
            if len(body["data"]) < PAGE_SIZE:
                break
            page_number += 1
        return products

    def upload_file(self, filename, content_b64, content_hash=None,
                    transaction_id=None):
        """POST a base64-encoded file to /files. Returns (guid, error_message).

        On success guid is the created file's GUID (from the response's
        data[].id); on failure guid is None and error_message explains why.

        `content_hash` is required by the API even though FilePost does not
        list it as required - without it every upload fails with "F002:
        Content hash is required." It is derived from the content when the
        caller does not pass one already computed.

        `transaction_id` is how an uploaded file is later linked to a product
        (or customer, or order): the same id is sent again on the PATCH that
        attaches it. Until that link is made the file's guid does not resolve -
        GET /files/{guid} 404s and a patch listing it answers "P023: File guid
        ... not found" - so an upload meant for a product must carry one.
        """
        if content_hash is None:
            try:
                content_hash = content_hash_b64(base64.b64decode(content_b64))
            except (ValueError, TypeError) as exc:
                # binascii.Error subclasses ValueError. Returned, not raised:
                # every other failure here is, and the engine relies on that
                # to fail one image instead of the whole run.
                return None, f"Could not hash the file content: {exc}"
        try:
            body = {"filename": filename, "content": content_b64,
                    "content_hash": content_hash}
            if transaction_id is not None:
                body["transaction_id"] = transaction_id
            resp = self.session.post(
                f"{API_BASE}/files",
                json=body,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return None, f"Connection error: {exc}"
        if not resp.ok:
            return None, f"HTTP {resp.status_code}: {resp.text[:500]}"
        try:
            data = resp.json().get("data") or []
        except ValueError:
            return None, "Malformed response from /files"
        if not data or not data[0].get("id"):
            return None, "No file GUID returned by /files"
        return data[0]["id"], ""

    def get_file(self, guid):
        """GET a single file's metadata by GUID. Returns (file_dict, error).

        Used to resolve an attached image's filename for display; the API has
        no way to expand a product's `files` array into full objects, so each
        GUID must be fetched individually. On failure file_dict is None.
        """
        try:
            resp = self.session.get(
                f"{API_BASE}/files/{guid}",
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return None, f"Connection error: {exc}"
        if not resp.ok:
            return None, f"HTTP {resp.status_code}: {resp.text[:500]}"
        try:
            data = resp.json().get("data") or []
        except ValueError:
            return None, "Malformed response from /files/{guid}"
        if not data:
            return None, "File not found"
        return data[0], ""

    def attach_files(self, product_code, file_guids, product_id=None,
                     transaction_id=None):
        """PATCH a product's `files` list. Returns (ok, error_message).

        Mirrors update_location: the API accepts updates only on the collection
        endpoint (PATCH /products) with an array of ProductPatch objects. Pass
        the full desired `files` list (the caller merges with any existing
        GUIDs so nothing already attached is lost).

        **Identify the product by `id` whenever it is known.** ProductPatch
        declares `id` required, and patching by `code` alone was rejected by a
        live instance while the identical id-keyed customer patch succeeded.
        `code` is only a fallback for a product that arrived without an id.

        GUIDs are sent as strings: `Product.files` items are declared strings
        while `File.id` - what POST /files hands back - is declared an integer,
        so a bare int can otherwise reach a string-typed array.

        `transaction_id` must be the one the files were uploaded under - it is
        what actually links them to the product ("The transaction id associated
        with files in order to link files to a product"). Listing the guids
        without it is rejected with "P023: File guid ... not found", because an
        unlinked file's guid does not resolve.
        """
        key = ({"id": product_id} if product_id is not None
               else {"code": product_code})
        patch = dict(key, files=[str(g) for g in file_guids])
        if transaction_id is not None:
            patch["transaction_id"] = transaction_id
        try:
            resp = self.session.patch(
                f"{API_BASE}/products",
                json=[patch],
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return False, f"Connection error: {exc}"
        if resp.ok:
            return True, ""
        keyed = "id" if product_id is not None else "code"
        return False, (f"HTTP {resp.status_code} (patched by {keyed}): "
                       f"{resp.text[:500]}")
