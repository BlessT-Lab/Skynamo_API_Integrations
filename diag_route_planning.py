"""Establish the facts route planning depends on, against a live instance.

The Public API spec leaves the things a route planner needs most undocumented:
the format of the instance's time zone offset, how scheduled-visit times are
interpreted, whether a batch POST is all-or-nothing, the order the interaction
history comes back in, and how big it is. Every one of those decides whether
the planner's write path can be trusted, so this measures them before anything
is written for real - the way diag_image_upload.py settled content_hash.

Two phases, and it stops before anything you have not agreed to:

  1. READ-ONLY. Configuration, users, visit frequencies, customers, the
     interaction history (first, second and last page only) and recent
     scheduled visits - printed with their raw shapes.
  2. WRITE PROBE (asks first; skipped unless you say yes). For a rep and
     customer you choose, creates a handful of scheduled visits for tomorrow,
     reads them back, then DELETES every one of them - whatever happens in
     between. It tells you exactly which ids to remove by hand if a delete
     fails. Use a test rep, or your own user, on a training instance.

The API key is redacted from all output, so this is safe to paste.

Usage:
    py diag_route_planning.py              # read-only, then asks about writes
    py diag_route_planning.py --read-only  # never offers the write probe
"""

import getpass
import json
import secrets
import sys
from collections import Counter
from datetime import date, datetime, timedelta

import requests

from skynamo_geo.client import SkynamoClient, create_date_filter
from skynamo_geo.config import API_BASE, PAGE_SIZE, REQUEST_TIMEOUT
from skynamo_geo.customers import has_coordinates

_SECRET = {"value": ""}


def redact(text):
    """Blank the API key wherever it appears."""
    if _SECRET["value"] and text:
        return text.replace(_SECRET["value"], "<API-KEY-REDACTED>")
    return text


def show(label, resp, limit=3000):
    """Print a response's status and its body, the key redacted."""
    print(f"    {label}")
    print(f"      HTTP {resp.status_code}")
    body = redact(resp.text or "")
    if not body:
        print("      body: (empty)")
        return
    try:
        print("      body: " + json.dumps(json.loads(body))[:limit])
    except ValueError:
        print("      body: " + body[:limit])


def ask(message, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"{message} {suffix} ").strip().lower()
    except EOFError:
        return default
    return default if not raw else raw in ("y", "yes")


def heading(text):
    print("\n" + "-" * 70)
    print(f" {text}")
    print("-" * 70)


# ---------------------------------------------------------------------------
# Phase 1: read-only
# ---------------------------------------------------------------------------

def read_configuration(client):
    heading("[1] /configurations - time zone and distance unit")
    cfg, error = client.get_configuration()
    if error:
        print(f"  ERROR: {redact(error)}")
        return {}
    print(f"  time_zone     = {json.dumps(cfg.get('time_zone'))}")
    print(f"  distance_unit = {json.dumps(cfg.get('distance_unit'))}")
    print("  (the utc_offset string format is undocumented - this is it)")
    return cfg


def read_users(client):
    heading("[2] /users")
    users, error = client.fetch_users()
    if error:
        print(f"  ERROR: {redact(error)}")
        return []
    active = [u for u in users if u.get("active", True)]
    print(f"  {len(users)} user(s), {len(active)} active")
    if users:
        print(f"  shape: keys = {sorted(users[0].keys())}")
    return users


def read_frequencies(client, customers_by_id):
    heading("[3] /visitfrequencies")
    freqs, error = client.fetch_visit_frequencies()
    if error:
        print(f"  ERROR: {redact(error)}")
        return []
    print(f"  {len(freqs)} record(s)")
    if not freqs:
        return []
    print(f"  shape: keys = {sorted(freqs[0].keys())}")
    shapes = Counter((f.get("frequency"), f.get("cycle"), f.get("period"))
                     for f in freqs)
    print("  (frequency, cycle, period) -> count:")
    for (fr, cy, pe), n in shapes.most_common(12):
        print(f"    {fr} per {cy} {pe}: {n}")
    pairs = Counter((f.get("customer_id"), f.get("user_id")) for f in freqs)
    dupes = sum(1 for n in pairs.values() if n > 1)
    print(f"  (customer, user) pairs with more than one record: {dupes}")
    unassigned = 0
    for f in freqs:
        cust = customers_by_id.get(f.get("customer_id"))
        if cust is not None and f.get("user_id") not in (cust.get("assigned_users") or []):
            unassigned += 1
    print(f"  records whose user is not in the customer's assigned_users: "
          f"{unassigned}")
    reps = Counter(f.get("user_id") for f in freqs)
    print(f"  reps with frequencies: {len(reps)}; most records for one rep: "
          f"{max(reps.values())}")
    return freqs


def read_customers(client):
    heading("[4] /customers - locations")
    customers, error = client._fetch_all("/customers")
    if error:
        print(f"  ERROR: {redact(error)}")
        return []
    with_loc = [c for c in customers if has_coordinates(c)]
    approx = [c for c in with_loc
              if (c.get("location") or {}).get("is_approximate")]
    print(f"  {len(customers)} customer(s): {len(with_loc)} with coordinates, "
          f"{len(customers) - len(with_loc)} without, {len(approx)} approximate")
    return customers


def read_interactions(client):
    heading("[5] /interactions - ordering and volume (no filters exist)")
    items, info, error = client.fetch_interactions_page(1)
    if error:
        print(f"  ERROR: {redact(error)}")
        return
    total = info.get("total_item_count")
    print(f"  total_item_count = {total}")
    if not total:
        print("  (no total reported - the full walk length is unknown)")
        return
    pages = -(-int(total) // PAGE_SIZE)
    print(f"  a full walk is {pages} request(s) of {PAGE_SIZE}")

    def describe(name, rows):
        if not rows:
            print(f"  {name}: empty")
            return None
        ids = [r.get("id") for r in rows]
        dates = [r.get("date") for r in rows]
        visits = sum(1 for r in rows if r.get("is_visit"))
        asc_ids = all(a is not None and b is not None and a <= b
                      for a, b in zip(ids, ids[1:]))
        asc_dates = all(a and b and a <= b for a, b in zip(dates, dates[1:]))
        print(f"  {name}: ids {ids[0]}..{ids[-1]} "
              f"({'ascending' if asc_ids else 'NOT ascending'}), dates "
              f"{dates[0]} .. {dates[-1]} "
              f"({'ascending' if asc_dates else 'NOT ascending'}), "
              f"{visits}/{len(rows)} are visits")
        return ids[0], ids[-1]

    first = describe("page 1", items)
    if items:
        print(f"  a raw date string: {items[0].get('date')!r}")
    if pages > 1:
        second, _info, _e = client.fetch_interactions_page(2)
        nxt = describe("page 2", second)
        last, _info, _e = client.fetch_interactions_page(pages)
        tail = describe(f"page {pages} (last)", last)
        if first and nxt and tail:
            across = first[1] <= nxt[0] <= tail[0]
            print(f"  ids ascending ACROSS pages: {across}")
            print("  -> if true, the planner can walk backwards from the last "
                  "page and stop early instead of reading everything.")


def read_scheduled_visits(client):
    heading("[6] /scheduledvisits - how times come back, and the filter syntax")
    since = date.today() - timedelta(days=30)
    visits, error = client.fetch_scheduled_visits(created_since=since)
    if error:
        print(f"  ERROR: {redact(error)}")
        return
    print(f"  {len(visits)} created since {since} (filter "
          f"{create_date_filter(since)})")
    for v in visits[:3]:
        print("    " + json.dumps({k: v.get(k) for k in (
            "id", "due_date", "end_time", "all_day", "reminder_offset",
            "completed_date", "assigned_user_id", "customer_id", "comment")}))

    # Does a space after the comma matter, and do two filters combine?
    for label, filt in (
        ("with a space", json.dumps(
            [f"greater_than_equals(create_date, {since.isoformat()})"])),
        ("two filters", json.dumps(
            [f"greater_than_equals(create_date,{since.isoformat()})",
             "greater_than(id,0)"])),
    ):
        try:
            resp = client.session.get(
                f"{API_BASE}/scheduledvisits",
                params={"page_number": 1, "page_size": 1, "filters": filt},
                timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  {label}: request failed: {exc}")
            continue
        try:
            page = resp.json().get("page") or {}
        except ValueError:
            page = {}
        print(f"  {label}: HTTP {resp.status_code}, "
              f"filtered_item_count={page.get('filtered_item_count')}, "
              f"total_item_count={page.get('total_item_count')}")


# ---------------------------------------------------------------------------
# Phase 2: write probe
# ---------------------------------------------------------------------------

def choose(users, freqs, customers_by_id):
    """Have the operator pick a rep and a customer to probe with."""
    print("\n  Some users:")
    for u in users[:12]:
        print(f"    id={u.get('id')}  {u.get('display_name') or u.get('user_name')}")
    try:
        raw = input("  User id to probe with (blank to skip): ").strip()
    except EOFError:
        return None, None
    if not raw.isdigit():
        return None, None
    user = next((u for u in users if u.get("id") == int(raw)), None)
    if user is None:
        print("  No such user.")
        return None, None
    theirs = [f for f in freqs if f.get("user_id") == user["id"]]
    if theirs:
        print("  Customers with a visit frequency for that user:")
        for f in theirs[:8]:
            print(f"    code={f.get('customer_code')}  {f.get('customer_name')}")
    try:
        code = input("  Customer code to probe with: ").strip()
    except EOFError:
        return None, None
    customer = next((c for c in customers_by_id.values()
                     if (c.get("code") or "") == code), None)
    if customer is None:
        print("  No such customer.")
        return None, None
    return user, customer


def write_probe(client, users, freqs, customers_by_id):
    heading("WRITE PROBE")
    user, customer = choose(users, freqs, customers_by_id)
    if user is None:
        print("  Skipped - nothing was written.")
        return

    tomorrow = date.today() + timedelta(days=1)
    local_offset = datetime.now().astimezone().strftime("%z")
    offset = f"{local_offset[:3]}:{local_offset[3:]}"
    run = secrets.token_hex(3)
    print(f"\n  Will create scheduled visits for {tomorrow} for "
          f"{user.get('display_name')} at {customer.get('code')}, then delete "
          f"them. Run token: RPPROBE-{run}")
    if not ask("  Go ahead?", default=False):
        print("  Skipped - nothing was written.")
        return

    created = []

    def post(label, bodies):
        print(f"\n  POST /scheduledvisits - {label}")
        try:
            resp = client.session.post(f"{API_BASE}/scheduledvisits",
                                       json=bodies, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            print(f"    request failed: {exc}")
            return
        show(label, resp)
        try:
            data = resp.json().get("data") or []
        except (ValueError, AttributeError):
            data = []
        for item in data:
            if isinstance(item, dict) and item.get("id") is not None:
                created.append(item["id"])
        sent = [b.get("comment") for b in bodies]
        echoed = [d.get("comment") for d in data if isinstance(d, dict)]
        print(f"    comments sent    : {sent}")
        print(f"    comments returned: {echoed}")
        print(f"    -> comment echoed: {bool(echoed)}; "
              f"same order: {echoed == sent[:len(echoed)]}")

    def body(tag, start, end, names=False, customer_id=None):
        b = {"due_date": start, "end_time": end, "all_day": False,
             "customer_id": customer_id if customer_id is not None else customer["id"],
             "assigned_user_id": user["id"],
             "comment": f"Route planning probe - safe to ignore. RPPROBE-{run}-{tag}"}
        if names:
            b["customer_code"] = customer.get("code")
            b["assigned_user_name"] = user.get("user_name")
        return b

    try:
        # a) explicit offset vs naive local - which does the server mean?
        post("two visits: explicit offset and naive time, ids only", [
            body("a", f"{tomorrow}T09:00:00{offset}",
                 f"{tomorrow}T09:30:00{offset}"),
            body("b", f"{tomorrow}T10:00:00", f"{tomorrow}T10:30:00"),
        ])
        # b) is a batch with one bad row all-or-nothing? (and do names work?)
        post("one valid (ids + names) and one invalid customer", [
            body("c", f"{tomorrow}T11:00:00{offset}",
                 f"{tomorrow}T11:30:00{offset}", names=True),
            body("d", f"{tomorrow}T12:00:00{offset}",
                 f"{tomorrow}T12:30:00{offset}", customer_id=-1),
        ])

        print("\n  Reading back what was created:")
        for visit_id in created:
            visit, error, status = client.get_scheduled_visit(visit_id)
            if visit is None:
                print(f"    id={visit_id}: HTTP {status} {redact(error)}")
                continue
            print("    " + json.dumps({k: visit.get(k) for k in (
                "id", "comment", "due_date", "end_time", "all_day",
                "reminder_offset", "assigned_user_id", "customer_id")}))
        print("\n  >> Look at the rep's calendar in Skynamo now: what TIME do "
              "visits 'a' (09:00 with offset) and 'b' (10:00 naive) show? "
              "And did the rep's device get a notification?")
        try:
            input("  Press Enter when you have looked, to delete them... ")
        except EOFError:
            pass
    finally:
        if created:
            print(f"\n  DELETE /scheduledvisits {created}")
            try:
                resp = client.session.delete(f"{API_BASE}/scheduledvisits",
                                             json=created,
                                             timeout=REQUEST_TIMEOUT)
                show("delete", resp)
            except requests.RequestException as exc:
                print(f"    request failed: {exc}")
            left = []
            for visit_id in created:
                visit, _error, status = client.get_scheduled_visit(visit_id)
                gone = visit is None
                print(f"    id={visit_id}: {'gone' if gone else 'STILL THERE'} "
                      f"(HTTP {status})")
                if not gone:
                    left.append(visit_id)
            if left:
                print(f"\n  !! Remove these by hand in Skynamo: {left}")
        else:
            print("\n  Nothing was created, so nothing to delete.")


def main():
    read_only = "--read-only" in sys.argv[1:]
    print("=" * 70)
    print(" Skynamo route planning - live probe")
    print("=" * 70)
    try:
        instance = input("Instance name: ").strip()
        key = getpass.getpass("Skynamo API key (not echoed): ").strip()
    except (EOFError, OSError):
        sys.exit("No input available. Run this in a terminal.")
    if not (instance and key):
        sys.exit("Both an instance name and an API key are required.")
    _SECRET["value"] = key
    client = SkynamoClient(instance, key)
    ok, message = client.test_connection()
    print(f"  connection: {'OK' if ok else 'FAIL'} - {redact(message)}")
    if not ok:
        sys.exit(1)

    read_configuration(client)
    users = read_users(client)
    customers = read_customers(client)
    customers_by_id = {c.get("id"): c for c in customers}
    freqs = read_frequencies(client, customers_by_id)
    read_interactions(client)
    read_scheduled_visits(client)

    if not read_only and users and customers:
        if ask("\nRun the write probe (creates and then deletes test visits)?",
               default=False):
            write_probe(client, users, freqs, customers_by_id)

    print("\n" + "=" * 70)
    print(" Paste this whole output - the API key is redacted.")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
