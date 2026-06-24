"""Helpers for reading and combining Skynamo customer address fields."""


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


def build_address(customer, address_fields):
    """Combine the mapped fields into one address string. Empty parts are dropped."""
    parts = [get_custom_field_value(customer, name) for name in address_fields]
    return ", ".join(p for p in parts if p)


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
