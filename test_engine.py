"""Smoke tests for the engine: preview produces correct plans and performs
no writes; write_locations only PATCHes included+writable rows."""

from skynamo_geo import engine
from skynamo_geo.geocoder import GeocodeResult
from skynamo_geo.config import (
    STATUS_PENDING, STATUS_SKIPPED_HAS_COORDS, STATUS_SKIPPED_NO_ADDRESS,
    STATUS_GEOCODE_FAILED, STATUS_UPDATED, STATUS_UPDATED_LOW_CONF,
)


class FakeGeocoder:
    """Returns ROOFTOP for known streets, APPROXIMATE for 'Town', None else."""
    def __init__(self):
        self.calls = []

    def geocode(self, address, country=None):
        self.calls.append(address)
        if "Main" in address:
            return GeocodeResult(-33.9, 18.4, "ROOFTOP", "1 Main Rd", False)
        if "Town" in address:
            return GeocodeResult(-33.7, 19.0, "APPROXIMATE", "Town", False)
        return None


class FakeClient:
    def __init__(self):
        self.patched = []

    def update_location(self, cid, lat, lng, accuracy=None, is_approximate=False):
        self.patched.append((cid, lat, lng, accuracy, is_approximate))
        return True, ""


def cust(cid, fields=None, loc=None):
    c = {"id": cid, "name": f"C{cid}", "code": f"K{cid}",
         "custom_fields": [{"name": k, "value": v} for k, v in (fields or {}).items()]}
    if loc:
        c["location"] = loc
    return c


customers = [
    cust(1, {"Street": "1 Main Rd", "City": "Cape Town"}),          # ROOFTOP precise
    cust(2, {"Street": "", "City": "Town"}),                        # APPROXIMATE low-conf
    cust(3, {"Street": "", "City": ""}),                            # no address
    cust(4, {"Street": "Nowhere"}, ),                               # geocode fail (None)
    cust(5, {"Street": "1 Main Rd"}, loc={"latitude": -33.9, "longitude": 18.4}),  # has coords
]

geo = FakeGeocoder()
plans = engine.geocode_customers(geo, customers, ["Street", "City"],
                                 replace_existing=False)

by_id = {p.customer_id: p for p in plans}
assert by_id[1].status == STATUS_PENDING and not by_id[1].low_confidence
assert by_id[1].include is True
assert by_id[2].status == STATUS_PENDING and by_id[2].low_confidence
assert by_id[3].status == STATUS_SKIPPED_NO_ADDRESS and by_id[3].include is False
assert by_id[4].status == STATUS_GEOCODE_FAILED and by_id[4].include is False
assert by_id[5].status == STATUS_SKIPPED_HAS_COORDS
# Customer 5 should NOT have been geocoded at all (skipped before geocode)
assert "1 Main Rd, Cape Town" in geo.calls  # cust 1 (combined fields)
assert "Town" in geo.calls                  # cust 2
assert len(geo.calls) == 3                   # custs 1, 2, 4 only (3 & 5 skipped)

# Preview performs NO writes: prove by writing with a fresh client and checking
# only the included rows (1 and 2) get PATCHed.
client = FakeClient()
# Deselect the low-confidence row 2 to simulate user review
by_id[2].include = False
report = engine.write_locations(client, plans)
patched_ids = [row[0] for row in client.patched]
assert patched_ids == [1], patched_ids  # only row 1 written
assert by_id[1].status == STATUS_UPDATED
# Row 1 accuracy must be the ROOFTOP value (10), is_approximate False
assert client.patched[0][3] == 10 and client.patched[0][4] is False

# Re-include row 2 and write again -> it should PATCH with APPROXIMATE/low-conf
client2 = FakeClient()
by_id[2].include = True
engine.write_locations(client2, [by_id[2]])
assert client2.patched[0][0] == 2
assert client2.patched[0][4] is True  # is_approximate
assert by_id[2].status == STATUS_UPDATED_LOW_CONF

# Cancel stops geocoding early
geo2 = FakeGeocoder()
stop_after = {"n": 0}
def cancel():
    stop_after["n"] += 1
    return stop_after["n"] > 2
plans2 = engine.geocode_customers(geo2, customers, ["Street", "City"],
                                  should_cancel=cancel)
assert len(plans2) <= 3, len(plans2)

print("All engine smoke tests passed")
