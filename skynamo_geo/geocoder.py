"""Geocoding providers.

`Geocoder` is the abstraction the engine depends on. `GoogleGeocoder` is the
only implementation today; adding Nominatim/Mapbox later means writing another
subclass and changing nothing in the engine.
"""

import time

import requests

from .config import (
    ACCURACY_BY_PRECISION, DEFAULT_ACCURACY, GEOCODE_URL,
    LOW_CONFIDENCE_PRECISIONS, REQUEST_TIMEOUT,
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
