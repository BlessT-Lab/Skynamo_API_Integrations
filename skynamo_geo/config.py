"""Shared constants for the Skynamo geolocation toolkit."""

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200  # API maximum
REQUEST_TIMEOUT = 30
# The Public API documents no rate limit, but route planning walks the whole
# interaction history (GET /interactions takes no filter at all), which can be
# thousands of requests. Back off on 429/503 rather than fail the run.
PUBLIC_API_MAX_RETRIES = 3
PUBLIC_API_BACKOFF_SECONDS = 2.0

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim usage policy: identify your app and stay at/below 1 request/second.
NOMINATIM_USER_AGENT = "SkynamoGeo/2.10 (Skynamo customer geolocation updater)"
NOMINATIM_MIN_INTERVAL = 1.0
# Ask Nominatim for several candidates so we can pick the most precise one
# instead of blindly trusting the first result.
NOMINATIM_RESULT_LIMIT = 5

# --- Address field roles -------------------------------------------------
# Each mapped Skynamo field is tagged with the address component it holds.
# Roles drive (a) structured Nominatim queries and (b) a clean, correctly
# ordered single-line address for display/reports and as a query fallback.
ROLE_STREET = "street"
ROLE_CITY = "city"
ROLE_STATE = "state"
ROLE_POSTCODE = "postalcode"
ROLE_COUNTRY = "country"
ROLE_OTHER = "other"  # folded into the street line; not a structured key

# Canonical order components appear in a single-line address.
ADDRESS_ROLES = [ROLE_STREET, ROLE_OTHER, ROLE_CITY, ROLE_STATE,
                 ROLE_POSTCODE, ROLE_COUNTRY]
# Structured keys Nominatim understands (ROLE_OTHER folds into street).
STRUCTURED_ROLES = [ROLE_STREET, ROLE_CITY, ROLE_STATE, ROLE_POSTCODE,
                    ROLE_COUNTRY]
ADDRESS_ROLE_LABELS = {
    ROLE_STREET: "Street / building",
    ROLE_CITY: "City / town",
    ROLE_STATE: "State / province",
    ROLE_POSTCODE: "Postal code",
    ROLE_COUNTRY: "Country",
    ROLE_OTHER: "Other / address line",
}
DEFAULT_ROLE = ROLE_OTHER

# Values that are effectively empty and must never be sent to a geocoder.
# Compared case-insensitively after trimming.
JUNK_ADDRESS_VALUES = {"", "0", "-", "--", "n/a", "na", "none", "null", "."}

# Nominatim reports how precise a match is (its `addresstype`, bucketed by
# NominatimGeocoder); we translate that into a Skynamo "accuracy" value
# (metres) so downstream reports can trust precise pins and treat coarse
# ones as approximate.
#   OSM_BUILDING - a building/house/amenity-level match
#   OSM_ROAD     - a street-level match (no house number)
#   OSM_AREA     - suburb/town/region centroid
ACCURACY_BY_PRECISION = {
    "OSM_BUILDING": 25,
    "OSM_ROAD": 200,
    "OSM_AREA": 3000,
}
DEFAULT_ACCURACY = 3000  # used if the provider returns an unrecognised precision

# Match precisions we consider too coarse to trust without a human check.
LOW_CONFIDENCE_PRECISIONS = {"OSM_ROAD", "OSM_AREA"}

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

# --- Product image import ------------------------------------------------
# Skynamo accepts PNG and JPG/JPEG only. Extensions are checked case-insensitively
# and the file's leading bytes are sniffed so a mis-named file is caught too.
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
# Magic-byte signatures used by sniff_image_format (no Pillow dependency).
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"

# POST /files requires a content_hash ("F002: Content hash is required." if it
# is missing). The spec describes it only as "(base64string)" and never names
# the algorithm, so it lives here: base64 of the digest of the file's raw
# bytes. diag_image_upload.py probes the alternatives against a live instance.
FILE_HASH_ALGORITHM = "md5"

# A product code may contain characters that can't appear in a filename; each is
# represented by a hyphen. To match a filename back to its product we apply this
# same forward transform to every known product code (see products.py).
WINDOWS_RESERVED_CHARS = '/\\:*?"<>|'
FILENAME_ESCAPE_CHAR = "-"

# Image plan / report statuses
STATUS_IMG_PENDING = "pending-upload"        # matched, not yet uploaded
STATUS_IMG_UPLOADED = "uploaded"
STATUS_IMG_NO_MATCH = "no-matching-product"
STATUS_IMG_BAD_FORMAT = "unsupported-format"
STATUS_IMG_AMBIGUOUS = "ambiguous-match"
STATUS_IMG_UPLOAD_FAILED = "upload-failed"

# Every terminal outcome that means "this image did not make it in". Used to
# group reasons for display, so a bulk failure reads as one line per cause.
IMAGE_FAILURE_STATUSES = (
    STATUS_IMG_UPLOAD_FAILED, STATUS_IMG_NO_MATCH, STATUS_IMG_BAD_FORMAT,
    STATUS_IMG_AMBIGUOUS,
)

# --- Filing processed images ---------------------------------------------
# After an upload run the images can be moved out of the folder they came from
# into these subfolders, leaving the root holding only what the run did not
# deal with. Off by default: it rearranges the user's own files.
IMAGE_FOLDER_SUCCESS = "Successful"
IMAGE_FOLDER_FAILED = "Failed"

# Which subfolder each outcome files into. STATUS_IMG_PENDING is deliberately
# absent: an image that matched a product but was deselected - or that a
# cancelled run never reached - has not been processed, so it stays put and the
# folder keeps showing exactly what is still outstanding.
IMAGE_FOLDER_BY_STATUS = {
    STATUS_IMG_UPLOADED: IMAGE_FOLDER_SUCCESS,
    STATUS_IMG_UPLOAD_FAILED: IMAGE_FOLDER_FAILED,
    STATUS_IMG_NO_MATCH: IMAGE_FOLDER_FAILED,
    STATUS_IMG_AMBIGUOUS: IMAGE_FOLDER_FAILED,
    STATUS_IMG_BAD_FORMAT: IMAGE_FOLDER_FAILED,
}

# Attached-image (manage/remove) statuses. Skynamo has no delete endpoint, so
# "removing" an image means detaching its GUID from the product's files list.
STATUS_ATT_LOADED = "attached"               # currently on the product
STATUS_ATT_FETCH_FAILED = "fetch-failed"     # couldn't resolve the file's name
STATUS_ATT_DELETED = "removed"               # detached from the product
STATUS_ATT_DELETE_FAILED = "remove-failed"

# Product-image report CSV column order
IMAGE_REPORT_FIELDNAMES = [
    "filename", "product_code", "matched_product", "sequence", "status",
    "moved_to", "notes",
]

# Attached-image (manage/remove) report CSV column order
ATTACHED_IMAGE_REPORT_FIELDNAMES = [
    "product_code", "matched_product", "filename", "file_guid", "status", "notes",
]

# --- Route planning --------------------------------------------------------
# Distances are straight-line (haversine) x a road-winding factor at an
# average speed: free, offline, and close enough to put stops in the right
# ORDER. Drive times are estimates and are labelled as such.
ROUTE_DEFAULT_DAYS = 5
ROUTE_DEFAULT_WEEKDAYS = (0, 1, 2, 3, 4)      # Monday=0
ROUTE_DEFAULT_DAY_START = "08:00"
ROUTE_DEFAULT_MAX_HOURS = 9.0                  # travel + time on site
ROUTE_DEFAULT_MAX_STOPS = 12
ROUTE_DEFAULT_SERVICE_MINUTES = 30
ROUTE_DEFAULT_ROAD_FACTOR = 1.3
ROUTE_DEFAULT_SPEED_KMH = 50
ROUTE_MIN_LEG_MINUTES = 3                      # two shops in one mall are not 0 min apart
ROUTE_BALANCE_WEIGHT = 0.5                     # evens out long and short days
ROUTE_MAX_RADIUS_KM = 300                      # a pin further than this from the start
                                               # is almost always a bad coordinate
ROUTE_FAR_START_KM = 100                       # warn: start is far from every customer
ROUTE_APPROX_ACCURACY_M = 500                  # coarser than this = "approximate pin"

# How far back to look for scheduled visits that already cover a customer.
# GET /scheduledvisits filters on create_date only, never on due date, so a
# visit created longer ago than this for a date inside the plan is not seen.
ROUTE_SCHEDULED_LOOKBACK_DAYS = 180
# Hard cap on /interactions requests in one history walk (it has no filter).
ROUTE_MAX_HISTORY_REQUESTS = 2000

# Route stop statuses
STATUS_RT_PLANNED = "planned"
STATUS_RT_ALREADY = "already-scheduled"        # an open visit already covers it
STATUS_RT_NO_LOCATION = "no-location"          # customer has no coordinates
STATUS_RT_OUT_OF_RANGE = "out-of-range"        # too far from the rep's start
STATUS_RT_OVER_CAPACITY = "over-capacity"      # due, but the week is full
STATUS_RT_INVALID_FREQ = "invalid-frequency"   # cannot plan from the record
STATUS_RT_NOT_FOUND = "customer-not-found"     # inactive, deleted or not visible

ROUTE_REPORT_FIELDNAMES = [
    "rep", "user_id", "day", "sequence", "customer_id", "code", "name",
    "status", "arrive", "depart", "leg_km", "leg_minutes", "due_date",
    "overdue_days", "last_visit", "frequency", "latitude", "longitude",
    "approximate", "google_maps", "waze", "notes",
]
