"""Local persistence for non-secret settings only.

Config lives at %APPDATA%/SkynamoGeo/config.json (or ~/.skynamo_geo on
non-Windows). API keys are deliberately NOT persisted anywhere - the user
re-enters them each session. purge_saved_credentials() removes any keys that
earlier versions of the app stored in the OS keyring.

Nor is the instance name: it says whose data this PC works with. Things that
must still be told apart per instance (each rep's remembered start point, the
visit-history cache) are keyed by instance_key(), a salted one-way fingerprint.
scrub_instance_names() removes what older versions saved in the clear.
"""

import hashlib
import json
import os
import re
import secrets

try:
    import keyring
except ImportError:
    keyring = None

APP_NAME = "SkynamoGeo"
KEYRING_SERVICE = "SkynamoGeo"


def _config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def app_data_dir(*parts):
    """A folder under the app's data directory, created if missing.

    For things the app keeps between sessions that are not settings - route
    plan records, the visit-history cache. Never credentials.
    """
    path = os.path.join(_config_dir(), *parts)
    os.makedirs(path, exist_ok=True)
    return path


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


# --- Instance names --------------------------------------------------------
# Never stored. A fingerprint keys what must stay separate per instance.

INSTANCE_KEY_PREFIX = "i-"
_INSTANCE_KEY_RE = re.compile(r"^i-[0-9a-f]{16}$")


def _install_salt(config):
    """This install's random salt, created (in `config`) on first use.

    Salted so the same instance does not fingerprint identically on every PC.
    An instance name is guessable, so this is a fingerprint and not secrecy:
    it keeps the name from being read or listed off the disk, which is the aim.
    """
    salt = config.get("install_salt")
    if not isinstance(salt, str) or not salt:
        salt = secrets.token_hex(16)
        config["install_salt"] = salt
    return salt


def instance_key(instance, config):
    """A stable, one-way key for an instance name ("i-" + 16 hex digits).

    Case- and whitespace-insensitive, like the instance name itself. May add
    `install_salt` to `config` - the caller saves it with whatever it keys.
    """
    name = (instance or "").strip().lower()
    digest = hashlib.sha256(
        f"{_install_salt(config)}:{name}".encode("utf-8")).hexdigest()
    return INSTANCE_KEY_PREFIX + digest[:16]


def saved_instance_key(instance):
    """instance_key() against the saved config, saving a newly made salt.

    For keying something stored outside config.json (the history cache): the
    salt must outlive the session or tomorrow's key would not find today's file.
    """
    config = load_config()
    had_salt = bool(config.get("install_salt"))
    key = instance_key(instance, config)
    if not had_salt:
        save_config(config)
    return key


def is_instance_key(text):
    return bool(_INSTANCE_KEY_RE.match(str(text)))


def scrub_instance_names():
    """Remove instance names an older version saved. Returns the legacy name.

    - config.json: drops `instance_name`; re-keys `route_reps` from the plain
      name to its fingerprint, so remembered start points survive.
    - route_history/: deletes cache files named after an instance (only a
      cache - the next connect rebuilds it under the fingerprint).
    Safe every startup: a no-op once clean. The name is returned, not kept, so
    the caller can still purge a keyring entry an even older version stored
    under it.
    """
    config = load_config()
    changed = False
    legacy = config.pop("instance_name", None)
    if legacy is not None:
        changed = True
    reps = config.get("route_reps")
    if isinstance(reps, dict):
        for name in [k for k in reps if not is_instance_key(k)]:
            key = instance_key(name, config)
            remembered = reps.pop(name)
            reps.setdefault(key, remembered)
            changed = True
    if changed:
        save_config(config)
    history = os.path.join(_config_dir(), "route_history")
    if os.path.isdir(history):          # never created just to look
        for entry in os.listdir(history):
            stem, ext = os.path.splitext(entry)
            if ext == ".json" and not is_instance_key(stem):
                try:
                    os.remove(os.path.join(history, entry))
                except OSError:
                    pass
    return legacy or None


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
