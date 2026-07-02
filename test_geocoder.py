"""Offline smoke tests for provider selection and the OSM precision mapping.
No network calls - only the factory and pure mapping logic."""

from skynamo_geo.config import (
    ACCURACY_BY_PRECISION, LOW_CONFIDENCE_PRECISIONS, GEOCODER_PROVIDERS,
)
from skynamo_geo.geocoder import (
    GoogleGeocoder, NominatimGeocoder, GeocodeError, GeocodeResult,
    create_geocoder, osm_precision,
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

print("All geocoder tests passed")
