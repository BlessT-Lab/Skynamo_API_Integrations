"""Local persistence: non-secret config in JSON, secrets in the OS keyring.

Config lives at %APPDATA%/SkynamoGeo/config.json (or ~/.skynamo_geo on
non-Windows). Secrets (API keys) are stored in the OS credential store via
keyring and are NEVER written to the JSON.
"""

import json
import os

try:
    import keyring
except ImportError:
    keyring = None

APP_NAME = "SkynamoGeo"
KEYRING_SERVICE = "SkynamoGeo"


def _config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def _config_path():
    return os.path.join(_config_dir(), "config.json")


def load_config():
    """Return the saved non-secret config dict (empty dict if none)."""
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


def save_config(config):
    """Persist the non-secret config dict. Returns the path written."""
    os.makedirs(_config_dir(), exist_ok=True)
    path = _config_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return path


# --- Secrets via keyring -------------------------------------------------

def secrets_available():
    return keyring is not None


def get_secret(name):
    """Fetch a stored secret by logical name, or None."""
    if not keyring:
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, name)
    except Exception:
        return None


def set_secret(name, value):
    """Store (or clear, if value is falsy) a secret by logical name."""
    if not keyring:
        return False
    try:
        if value:
            keyring.set_password(KEYRING_SERVICE, name, value)
        else:
            try:
                keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        return True
    except Exception:
        return False


def skynamo_key_name(instance):
    """Per-instance keyring name for the Skynamo API key."""
    return f"skynamo:{instance}" if instance else "skynamo"


GOOGLE_KEY_NAME = "google"
