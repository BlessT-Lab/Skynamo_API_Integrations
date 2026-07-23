"""Shared constants for the Skynamo geolocation toolkit."""

API_BASE = "https://api.skynamo.me/v1"
PAGE_SIZE = 200  # API maximum
REQUEST_TIMEOUT = 30

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim usage policy: identify your app and stay at/below 1 request/second.
NOMINATIM_USER_AGENT = "SkynamoGeo/2.5 (Skynamo customer geolocation updater)"
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

# Attached-image (manage/remove) statuses. Skynamo has no delete endpoint, so
# "removing" an image means detaching its GUID from the product's files list.
STATUS_ATT_LOADED = "attached"               # currently on the product
STATUS_ATT_FETCH_FAILED = "fetch-failed"     # couldn't resolve the file's name
STATUS_ATT_DELETED = "removed"               # detached from the product
STATUS_ATT_DELETE_FAILED = "remove-failed"

# Product-image report CSV column order
IMAGE_REPORT_FIELDNAMES = [
    "filename", "product_code", "matched_product", "sequence", "status", "notes",
]

# Attached-image (manage/remove) report CSV column order
ATTACHED_IMAGE_REPORT_FIELDNAMES = [
    "product_code", "matched_product", "filename", "file_guid", "status", "notes",
]
