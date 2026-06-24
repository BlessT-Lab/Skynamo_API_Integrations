"""Shared constants for the Skynamo geolocation toolkit."""

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200  # API maximum
REQUEST_TIMEOUT = 30

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GEOCODE_DELAY_SECONDS = 0.05  # Google allows ~50 req/s; a small delay is plenty

# Google's location_type tells us how precise the match is. We translate it
# into a Skynamo "accuracy" value (metres) so downstream reports can trust
# precise pins and treat coarse ones as approximate.
#   ROOFTOP            - exact street address
#   RANGE_INTERPOLATED - interpolated between two known points on a road
#   GEOMETRIC_CENTER   - centre of a street/polyline (e.g. a road, not a number)
#   APPROXIMATE        - region/locality centroid (town or suburb level)
ACCURACY_BY_PRECISION = {
    "ROOFTOP": 10,
    "RANGE_INTERPOLATED": 50,
    "GEOMETRIC_CENTER": 200,
    "APPROXIMATE": 3000,
}
DEFAULT_ACCURACY = 3000  # used if Google returns an unrecognised precision

# Match precisions we consider too coarse to trust without a human check.
LOW_CONFIDENCE_PRECISIONS = {"APPROXIMATE", "GEOMETRIC_CENTER"}

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
