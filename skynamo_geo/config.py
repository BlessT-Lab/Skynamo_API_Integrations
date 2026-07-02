"""Shared constants for the Skynamo geolocation toolkit."""

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200  # API maximum
REQUEST_TIMEOUT = 30

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_DELAY_SECONDS = 0.05  # Google allows ~50 req/s; a small delay is plenty

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim usage policy: identify your app and stay at/below 1 request/second.
NOMINATIM_USER_AGENT = "SkynamoGeo/2.1 (Skynamo customer geolocation updater)"
NOMINATIM_MIN_INTERVAL = 1.0

# Geocoding providers selectable in the GUI/CLI. Keys are the internal ids
# used in saved config; values are the labels shown to the user.
GEOCODER_PROVIDERS = {
    "google": "Google Maps",
    "osm": "OpenStreetMap",
}
DEFAULT_PROVIDER = "google"

# Each provider reports how precise a match is; we translate that into a
# Skynamo "accuracy" value (metres) so downstream reports can trust precise
# pins and treat coarse ones as approximate.
# Google location_type:
#   ROOFTOP            - exact street address
#   RANGE_INTERPOLATED - interpolated between two known points on a road
#   GEOMETRIC_CENTER   - centre of a street/polyline (e.g. a road, not a number)
#   APPROXIMATE        - region/locality centroid (town or suburb level)
# OpenStreetMap/Nominatim addresstype, bucketed by NominatimGeocoder:
#   OSM_BUILDING       - a building/house/amenity-level match
#   OSM_ROAD           - a street-level match (no house number)
#   OSM_AREA           - suburb/town/region centroid
ACCURACY_BY_PRECISION = {
    "ROOFTOP": 10,
    "RANGE_INTERPOLATED": 50,
    "GEOMETRIC_CENTER": 200,
    "APPROXIMATE": 3000,
    "OSM_BUILDING": 25,
    "OSM_ROAD": 200,
    "OSM_AREA": 3000,
}
DEFAULT_ACCURACY = 3000  # used if the provider returns an unrecognised precision

# Match precisions we consider too coarse to trust without a human check.
LOW_CONFIDENCE_PRECISIONS = {"APPROXIMATE", "GEOMETRIC_CENTER",
                             "OSM_ROAD", "OSM_AREA"}

# Report / plan statuses
STATUS_UPDATED = "updated"
STATUS_UPDATED_LOW_CONF = "updated-low-confidence"
STATUS_SKIPPED_HAS_COORDS = "skipped-has-coordinates"
STATUS_SKIPPED_NO_ADDRESS = "skipped-no-address"
STATUS_GEOCODE_FAILED = "geocode-failed"
STATUS_UPDATE_FAILED = "update-failed"
STATUS_PENDING = "pending-write"  # preview produced coords, not yet written

# Report CSV column order (shared by GUI and CLI)
REPORT_FIELDNAMES = [
    "customer_id", "code", "name", "status", "address_used",
    "latitude", "longitude", "accuracy", "match_precision", "notes",
]
