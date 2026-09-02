"""Skynamo toolkit.

UI-agnostic core plus front-ends (CLI in skynamo_geolocation.py, GUI in
gui.py) that share it. Talks to two separate Skynamo APIs:

  Public API    (client.py)           - read/write: customers, products, files
  Reporting API (reporting_client.py) - read-only analytics, OAuth2

Features: customer geolocation, product-image import/management, reporting
extracts into a local store, and HTML dashboards built from that store.
"""

__version__ = "2.10.3"
