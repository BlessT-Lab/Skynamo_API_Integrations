"""Helpers for reading and combining Skynamo customer address fields.

Address fields are mapped to *roles* (street/city/state/postcode/country, or
"other"). `build_query` turns a customer plus that mapping into an
`AddressQuery` carrying both a clean single-line `text` (a fallback query and
for display/reports) and a `structured` dict (for a more accurate Nominatim
structured search).
"""

import re

from .config import (
    ADDRESS_ROLES, JUNK_ADDRESS_VALUES, ROLE_OTHER, ROLE_STREET,
    STRUCTURED_ROLES,
)

_WHITESPACE = re.compile(r"\s+")


def get_custom_field_value(customer, field_name):
    for field in customer.get("custom_fields") or []:
        if field.get("name") == field_name:
            return (field.get("value") or "").strip()
    return ""


def collect_custom_field_names(customers):
    """Unique custom field names across all customers, in first-seen order."""
    names = []
    seen = set()
    for customer in customers:
        for field in customer.get("custom_fields") or []:
            name = field.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def clean_value(value):
    """Normalise a field value; return "" for junk/placeholder values.

    Collapses internal whitespace and drops values that are effectively empty
    ("0", "N/A", "-", etc.) so they never pollute a geocoding query.
    """
    if not value:
        return ""
    collapsed = _WHITESPACE.sub(" ", str(value)).strip()
    if collapsed.lower() in JUNK_ADDRESS_VALUES:
        return ""
    return collapsed


class AddressQuery:
    """A geocodable address in two forms.

    text       - clean single-line string (fallback query + display/report)
    structured - {role: value} for Nominatim structured search; may be empty
                 when the mapping has no recognised structured roles.
    """

    def __init__(self, text, structured):
        self.text = text
        self.structured = structured

    def __bool__(self):
        return bool(self.text)


def build_query(customer, field_roles):
    """Build an AddressQuery from a customer and an ordered field->role list.

    field_roles is a list of (field_name, role) tuples. Values are cleaned;
    fields sharing a role are combined (space for the street line, comma
    otherwise). ROLE_OTHER values fold into the street component. The
    single-line text is assembled in canonical ADDRESS_ROLES order.
    """
    by_role = {}
    for name, role in field_roles:
        value = clean_value(get_custom_field_value(customer, name))
        if value:
            by_role.setdefault(role, []).append(value)

    # ROLE_OTHER folds into the street line for both structured and text forms.
    street_parts = by_role.get(ROLE_STREET, []) + by_role.get(ROLE_OTHER, [])

    structured = {}
    for role in STRUCTURED_ROLES:
        if role == ROLE_STREET:
            if street_parts:
                structured[ROLE_STREET] = " ".join(street_parts)
        elif by_role.get(role):
            structured[role] = ", ".join(by_role[role])

    text_parts = []
    for role in ADDRESS_ROLES:
        if role in (ROLE_STREET, ROLE_OTHER):
            continue  # handled together below
        text_parts.extend(by_role.get(role, []))
    text = ", ".join(street_parts + text_parts)

    return AddressQuery(text=text, structured=structured)


def has_coordinates(customer):
    """True only if the customer has real, non-zero coordinates.

    Missing, null, or zero latitude/longitude (including "0" stored as a
    string) all count as having NO coordinates, so such customers are
    geocoded and updated whenever they have an address - even when the
    replace-existing option is off.
    """
    location = customer.get("location") or {}

    def is_real(value):
        try:
            return float(value) != 0.0
        except (TypeError, ValueError):
            return False

    return is_real(location.get("latitude")) and is_real(location.get("longitude"))
