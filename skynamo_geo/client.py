"""Skynamo public API client."""

import requests

from .config import API_BASE, PAGE_SIZE, REQUEST_TIMEOUT, DEFAULT_ACCURACY


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
            return False, f"Unexpected response: HTTP {resp.status_code} - {resp.text[:200]}"
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
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"

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

    def upload_file(self, filename, content_b64):
        """POST a base64-encoded file to /files. Returns (guid, error_message).

        On success guid is the created file's GUID (from the response's
        data[].id); on failure guid is None and error_message explains why.
        """
        try:
            resp = self.session.post(
                f"{API_BASE}/files",
                json={"filename": filename, "content": content_b64},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            return None, f"Connection error: {exc}"
        if not resp.ok:
            return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
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
            return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
        try:
            data = resp.json().get("data") or []
        except ValueError:
            return None, "Malformed response from /files/{guid}"
        if not data:
            return None, "File not found"
        return data[0], ""

    def attach_files(self, product_code, file_guids, product_id=None):
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
        """
        key = ({"id": product_id} if product_id is not None
               else {"code": product_code})
        patch = dict(key, files=[str(g) for g in file_guids])
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
                       f"{resp.text[:200]}")
