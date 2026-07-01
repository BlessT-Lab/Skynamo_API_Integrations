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
