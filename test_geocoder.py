"""Offline smoke tests for provider selection, the OSM precision mapping,
role-based query building, value cleaning, and candidate ranking.
No network calls - only the factory and pure logic."""

from skynamo_geo.config import (
    ACCURACY_BY_PRECISION, LOW_CONFIDENCE_PRECISIONS, GEOCODER_PROVIDERS,
    ROLE_STREET, ROLE_CITY, ROLE_STATE, ROLE_POSTCODE, ROLE_COUNTRY, ROLE_OTHER,
)
from skynamo_geo.customers import clean_value, build_query, AddressQuery
from skynamo_geo.geocoder import (
    GoogleGeocoder, NominatimGeocoder, GeocodeError, GeocodeResult,
    create_geocoder, osm_precision, _pick_best, _query_parts,
)

# Factory: right class per provider id, key rules enforced
assert isinstance(create_geocoder("google", "fake-key"), GoogleGeocoder)
assert isinstance(create_geocoder("osm"), NominatimGeocoder)
try:
    create_geocoder("google")  # no key -> error
    assert False, "google without key should raise"
except GeocodeError:
    pass
try:
    create_geocoder("mapbox")
    assert False, "unknown provider should raise"
except GeocodeError:
    pass

# Both provider ids must be offered to users
assert set(GEOCODER_PROVIDERS) == {"google", "osm"}

# addresstype buckets
assert osm_precision("building") == "OSM_BUILDING"
assert osm_precision("house") == "OSM_BUILDING"
assert osm_precision("shop") == "OSM_BUILDING"
assert osm_precision("road") == "OSM_ROAD"
assert osm_precision("suburb") == "OSM_AREA"
assert osm_precision("town") == "OSM_AREA"
assert osm_precision("") == "OSM_AREA"

# Every OSM precision label must have an accuracy, and only OSM_BUILDING
# counts as high confidence
for label in ("OSM_BUILDING", "OSM_ROAD", "OSM_AREA"):
    assert label in ACCURACY_BY_PRECISION, label
assert "OSM_BUILDING" not in LOW_CONFIDENCE_PRECISIONS
assert {"OSM_ROAD", "OSM_AREA"} <= LOW_CONFIDENCE_PRECISIONS

# GeocodeResult derives accuracy/low-confidence from OSM labels too
precise = GeocodeResult(-33.9, 18.4, "OSM_BUILDING", "1 Main Rd", False)
assert precise.accuracy == 25 and not precise.is_low_confidence
coarse = GeocodeResult(-33.7, 19.0, "OSM_AREA", "Town", False)
assert coarse.accuracy == 3000 and coarse.is_low_confidence

# Nominatim client identifies itself per the usage policy
osm = NominatimGeocoder()
assert "SkynamoGeo" in osm.session.headers["User-Agent"]

# --- clean_value: strips junk/placeholder values ---
assert clean_value("  Main   Rd ") == "Main Rd"      # collapses whitespace
assert clean_value("0") == ""
assert clean_value("N/A") == ""
assert clean_value("none") == ""
assert clean_value(None) == ""
assert clean_value("Cape Town") == "Cape Town"

# --- build_query: structured dict + ordered single-line text ---
def cust(**fields):
    return {"custom_fields": [{"name": k, "value": v} for k, v in fields.items()]}

roles = [("Str", ROLE_STREET), ("Sub", ROLE_OTHER), ("City", ROLE_CITY),
         ("Prov", ROLE_STATE), ("Zip", ROLE_POSTCODE), ("Ctry", ROLE_COUNTRY)]
q = build_query(cust(Str="1 Main Rd", Sub="Bldg 5", City="Cape Town",
                     Prov="WC", Zip="8001", Ctry="South Africa"), roles)
assert isinstance(q, AddressQuery) and bool(q)
# ROLE_OTHER folds into the street component (structured) ...
assert q.structured[ROLE_STREET] == "1 Main Rd Bldg 5"
assert q.structured[ROLE_CITY] == "Cape Town"
assert q.structured[ROLE_POSTCODE] == "8001"
assert q.structured[ROLE_COUNTRY] == "South Africa"
# ... and text is ordered street, other, city, state, postcode, country
assert q.text == "1 Main Rd, Bldg 5, Cape Town, WC, 8001, South Africa", q.text

# Junk values drop out; empty mapping yields a falsy query with no structured keys
q2 = build_query(cust(Str="0", City="  "), [("Str", ROLE_STREET),
                                            ("City", ROLE_CITY)])
assert not q2 and q2.text == "" and q2.structured == {}

# A str or AddressQuery both split into (text, structured)
assert _query_parts("Cape Town") == ("Cape Town", {})
t, s = _query_parts(q)
assert t == q.text and s == q.structured

# --- _pick_best: prefers the most precise bucket, tie-breaks on importance ---
candidates = [
    {"addresstype": "suburb", "importance": 0.9, "lat": "1", "lon": "1"},
    {"addresstype": "house", "importance": 0.2, "lat": "2", "lon": "2"},
    {"addresstype": "road", "importance": 0.5, "lat": "3", "lon": "3"},
]
assert _pick_best(candidates)["addresstype"] == "house"  # building beats road/area
tie = [
    {"addresstype": "house", "importance": 0.3, "lat": "1", "lon": "1"},
    {"addresstype": "house", "importance": 0.7, "lat": "2", "lon": "2"},
]
assert _pick_best(tie)["importance"] == 0.7  # higher importance wins the tie

# GeocodeResult carries validation components
r = GeocodeResult(1.0, 2.0, "ROOFTOP", "addr", False,
                  country_code="za", postcode="8001")
assert r.country_code == "za" and r.postcode == "8001"

print("All geocoder tests passed")
