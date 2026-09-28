import json
import subprocess
from pathlib import Path

from personaforge import load_catalog, load_manifest, resolve_profile_path, resolve_runtime_plan


CATALOG_PATH = Path("/usr/share/personaforge/catalogs/202605.yaml")
PROFILE_DIRS = (Path("/usr/share/personaforge/profiles"),)
LIVE_METADATA_PATH = Path("/run/personaforge/active.json")
PERSISTENT_METADATA_PATH = Path("/var/lib/personaforge/active.json")
SAVED_CONFIG_PATH = Path("/etc/sonic/config_db.json")


def config_tables(config_db):
    return {
        "FEATURE": config_db.get_table("FEATURE") or {},
        "FRR_DAEMON": config_db.get_table("FRR_DAEMON") or {},
    }


def load_plan(profile, config_db, profile_dirs=None, catalog_path=None):
    path = resolve_profile_path(profile, profile_dirs or PROFILE_DIRS)
    manifest = load_manifest(str(path))
    catalog = load_catalog(str(catalog_path or CATALOG_PATH))
    return resolve_runtime_plan(manifest, catalog, config_tables(config_db))


def verify_actions(config_db, actions):
    tables = config_tables(config_db)
    failures = []
    for action in actions:
        entry = (tables.get(action.table) or {}).get(action.key) or {}
        actual = entry.get(action.field)
        if actual != action.desired:
            failures.append("{}|{} {} expected {!r}, found {!r}".format(
                action.table, action.key, action.field, action.desired, actual
            ))
    if failures:
        raise RuntimeError("ConfigDB verification failed: {}".format("; ".join(failures)))


def restart_bgp(run=subprocess.run):
    result = run(["systemctl", "restart", "bgp"], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("BGP restart failed: {}".format(result.stderr.strip() or result.stdout.strip()))
    result = run(["systemctl", "is-active", "--quiet", "bgp"], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("BGP service did not become active")


def save_config(run=subprocess.run):
    result = run(["config", "save", "-y"], text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError("SONiC configuration save failed: {}".format(
            result.stderr.strip() or result.stdout.strip()))


def read_saved_tables(path=SAVED_CONFIG_PATH):
    try:
        with Path(path).open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except FileNotFoundError:
        return {}
    return {
        "FEATURE": document.get("FEATURE", {}),
        "FRR_DAEMON": document.get("FRR_DAEMON", {}),
    }
