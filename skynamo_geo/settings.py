"""Local persistence for non-secret settings only.

Config lives at %APPDATA%/SkynamoGeo/config.json (or ~/.skynamo_geo on
non-Windows). API keys are deliberately NOT persisted anywhere - the user
re-enters them each session. purge_saved_credentials() removes any keys that
earlier versions of the app stored in the OS keyring.
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


# --- Credential cleanup --------------------------------------------------
# API keys are intentionally NOT persisted. These names are kept only so we
# can delete keys that older versions of the app stored in the OS keyring.

GOOGLE_KEY_NAME = "google"


def skynamo_key_name(instance):
    """Keyring name an older version used for the Skynamo API key."""
    return f"skynamo:{instance}" if instance else "skynamo"


def purge_saved_credentials(instance=None):
    """Delete any API keys a previous version stored in the OS keyring.

    Safe to call every startup: missing entries and keyring errors are
    ignored. Clears the Google key, the generic Skynamo key, and (if known)
    the per-instance Skynamo key.
    """
    if not keyring:
        return
    names = {GOOGLE_KEY_NAME, skynamo_key_name(None), skynamo_key_name(instance)}
    for name in names:
        try:
            keyring.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass
