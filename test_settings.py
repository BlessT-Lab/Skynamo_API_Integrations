"""Offline tests for settings: the instance name is never kept on disk.

Runs against a temporary data folder, never the real %APPDATA%.
"""

import json
import os
import shutil
import tempfile

from skynamo_geo import settings

tmp = tempfile.mkdtemp(prefix="skynamo_settings_")
real_config_dir = settings._config_dir
try:
    settings._config_dir = lambda: tmp

    # --- the fingerprint ------------------------------------------------------
    cfg = {}
    key = settings.instance_key("Acme", cfg)
    assert settings.is_instance_key(key), key
    assert "acme" not in key.lower()
    assert cfg.get("install_salt"), "the salt is created on first use"
    # the same instance however it is typed; a different one differs
    assert settings.instance_key("  ACME ", cfg) == key
    assert settings.instance_key("acme2", cfg) != key
    # salted: the same name on another install keys differently
    assert settings.instance_key("acme", {"install_salt": "other"}) != key
    assert not settings.is_instance_key("acme")

    # saved_instance_key keeps the salt, so tomorrow finds today's cache file
    first = settings.saved_instance_key("acme")
    assert settings.saved_instance_key("acme") == first
    assert "acme" not in json.dumps(settings.load_config())

    # --- the scrub of what older versions saved ------------------------------
    shutil.rmtree(tmp)
    os.makedirs(os.path.join(tmp, "route_history"))
    history = os.path.join(tmp, "route_history")
    for name in ("acme.json", "i-0123456789abcdef.json", "notes.txt"):
        open(os.path.join(history, name), "w").close()
    settings.save_config({
        "instance_name": "acme",
        "country": "ZA",
        "route_reps": {
            "acme": {"7": {"address": "1 Main Rd", "include": True}},
        },
    })

    legacy = settings.scrub_instance_names()
    assert legacy == "acme", "returned once, for the keyring purge"
    saved = settings.load_config()
    assert "acme" not in json.dumps(saved), saved
    assert saved["country"] == "ZA", "other settings are untouched"
    # remembered start points survive, under the fingerprint
    reps = saved["route_reps"][settings.instance_key("acme", saved)]
    assert reps["7"]["address"] == "1 Main Rd"
    # a cache file named after the instance is removed; others are left alone
    assert sorted(os.listdir(history)) == ["i-0123456789abcdef.json", "notes.txt"]

    # idempotent: a clean config is not rewritten, and nothing more goes
    before = os.path.getmtime(os.path.join(tmp, "config.json"))
    assert settings.scrub_instance_names() is None
    assert os.path.getmtime(os.path.join(tmp, "config.json")) == before
    assert len(os.listdir(history)) == 2

    # with nothing saved at all, it creates nothing
    shutil.rmtree(tmp)
    assert settings.scrub_instance_names() is None
    assert not os.path.exists(tmp), "must not create the data folder to look"
finally:
    settings._config_dir = real_config_dir
    shutil.rmtree(tmp, ignore_errors=True)

print("All settings tests passed")
