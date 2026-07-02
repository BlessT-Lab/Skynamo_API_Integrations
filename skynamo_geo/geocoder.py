"""Geocoding providers.

`Geocoder` is the abstraction the engine depends on. Two implementations:
`GoogleGeocoder` (paid, most accurate) and `NominatimGeocoder` (OpenStreetMap,
free, no API key, throttled to 1 req/s per its usage policy). Use
`create_geocoder(provider, api_key)` to construct one from a provider id.
"""

import time

import requests

from .config import (
    ACCURACY_BY_PRECISION, DEFAULT_ACCURACY, GEOCODE_URL,
    LOW_CONFIDENCE_PRECISIONS, NOMINATIM_MIN_INTERVAL, NOMINATIM_URL,
    NOMINATIM_USER_AGENT, REQUEST_TIMEOUT,
)


class GeocodeResult:
    """Outcome of a single successful geocode."""

    def __init__(self, lat, lng, precision, formatted_address, partial_match):
        self.lat = lat
        self.lng = lng
        self.precision = precision  # provider precision label (Google location_type)
        self.formatted_address = formatted_address
        self.partial_match = partial_match

    @property
    def accuracy(self):
        return ACCURACY_BY_PRECISION.get(self.precision, DEFAULT_ACCURACY)

    @property
    def is_low_confidence(self):
        """True if the match is coarse or only partially matched the input."""
        return self.partial_match or self.precision in LOW_CONFIDENCE_PRECISIONS


class GeocodeError(Exception):
    """Raised for fatal provider errors (bad key, billing, quota)."""


class Geocoder:
    """Base class. Implementations return a GeocodeResult or None (not found)."""

    def geocode(self, address, country=None):
        raise NotImplementedError

    def validate(self, country=None):
        """Probe the provider with a known address; raise GeocodeError if misconfigured."""
        self.geocode("Cape Town", country=country)


class GoogleGeocoder(Geocoder):
    def __init__(self, api_key, retries=2):
        self.api_key = api_key
        self.retries = retries
        self.session = requests.Session()

    def geocode(self, address, country=None):
        """Geocode one address via Google. Returns a GeocodeResult or None.

        None means the address simply could not be found (ZERO_RESULTS).
        A GeocodeError is raised for configuration problems (REQUEST_DENIED)
        so the caller can stop rather than hammering the API for every row.
        """
        params = {"address": address, "key": self.api_key}
        if country:
            # Restrict results to the given country (ISO 3166-1 alpha-2 code).
            params["components"] = f"country:{country}"
            params["region"] = country.lower()

        for attempt in range(self.retries + 1):
            try:
                resp = self.session.get(GEOCODE_URL, params=params,
                                        timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                body = resp.json()
            except requests.RequestException:
                if attempt < self.retries:
                    time.sleep(2)
                    continue
                return None

            status = body.get("status")
            if status == "OK":
                top = body["results"][0]
                loc = top["geometry"]["location"]
                return GeocodeResult(
                    lat=loc["lat"],
                    lng=loc["lng"],
                    precision=top["geometry"].get("location_type", ""),
                    formatted_address=top.get("formatted_address", ""),
                    partial_match=top.get("partial_match", False),
                )
            if status == "ZERO_RESULTS":
                return None
            if status == "OVER_QUERY_LIMIT":
                if attempt < self.retries:
                    time.sleep(2)
                    continue
                raise GeocodeError(
                    "Google API rate/quota limit hit (OVER_QUERY_LIMIT). "
                    "Check your billing and quota.")
            # REQUEST_DENIED, INVALID_REQUEST, etc. - not worth retrying.
            raise GeocodeError(
                f"Google API error: {status} - "
                f"{body.get('error_message', 'no detail')}")
        return None


# Nominatim `addresstype` values bucketed into our precision labels.
# Anything not listed falls through to OSM_AREA (suburb/town/region level).
_OSM_BUILDING_TYPES = {
    "building", "house", "residential", "apartments", "detached",
    "amenity", "shop", "office", "industrial", "retail", "commercial",
    "tourism", "leisure", "craft", "place_of_worship", "school",
}
_OSM_ROAD_TYPES = {
    "road", "street", "highway", "pedestrian", "footway",
    "cycleway", "path", "track", "service",
}


def osm_precision(addresstype):
    """Map a Nominatim addresstype to one of our precision labels."""
    if addresstype in _OSM_BUILDING_TYPES:
        return "OSM_BUILDING"
    if addresstype in _OSM_ROAD_TYPES:
        return "OSM_ROAD"
    return "OSM_AREA"


class NominatimGeocoder(Geocoder):
    """OpenStreetMap's free geocoder (Nominatim). No API key required.

    The public usage policy requires an identifying User-Agent and at most
    one request per second; this class throttles itself so callers (and the
    engine's own delay) don't need to know about it.
    """

    def __init__(self, retries=2):
        self.retries = retries
        self.session = requests.Session()
        self.session.headers["User-Agent"] = NOMINATIM_USER_AGENT
        self._last_request = 0.0

    def _throttle(self):
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def geocode(self, address, country=None):
        """Geocode one address via Nominatim. Returns a GeocodeResult or None.

        None means not found or a transient network failure after retries.
        A GeocodeError is raised when Nominatim blocks us (403/429 that
        persists), so the caller stops instead of hammering the service.
        """
        params = {"q": address, "format": "jsonv2", "limit": 1}
        if country:
            params["countrycodes"] = country.lower()

        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                resp = self.session.get(NOMINATIM_URL, params=params,
                                        timeout=REQUEST_TIMEOUT)
            except requests.RequestException:
                if attempt < self.retries:
                    time.sleep(2)
                    continue
                return None
            if resp.status_code in (403, 429):
                if attempt < self.retries:
                    time.sleep(5)
                    continue
                raise GeocodeError(
                    f"Nominatim blocked the request (HTTP {resp.status_code}). "
                    "The free OpenStreetMap service rate-limits heavy use - "
                    "wait a while and retry, or switch to Google Maps.")
            try:
                resp.raise_for_status()
                results = resp.json()
            except (requests.RequestException, ValueError):
                if attempt < self.retries:
                    time.sleep(2)
                    continue
                return None

            if not results:
                return None
            top = results[0]
            return GeocodeResult(
                lat=float(top["lat"]),
                lng=float(top["lon"]),
                precision=osm_precision(top.get("addresstype")
                                        or top.get("type", "")),
                formatted_address=top.get("display_name", ""),
                partial_match=False,
            )
        return None


def create_geocoder(provider, api_key=None):
    """Build a Geocoder from a provider id ('google' or 'osm')."""
    if provider == "osm":
        return NominatimGeocoder()
    if provider == "google":
        if not api_key:
            raise GeocodeError("Google Maps requires an API key.")
        return GoogleGeocoder(api_key)
    raise GeocodeError(f"Unknown geocoding provider: {provider!r}")
