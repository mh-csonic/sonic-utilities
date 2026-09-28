import copy
import click

from personaforge import (
    PersonaForgeContractError,
    RuntimeContractError,
    atomic_write_json,
    calculate_drift,
    create_active_metadata,
    inverse_actions,
    read_metadata,
)
from utilities_common.cli import AbbreviationGroup, pass_db
from utilities_common import personaforge as common

from .validated_config_db_connector import ValidatedConfigDBConnector


def _write_action(connector, action):
    if action.desired is None:
        connector.set_entry(action.table, action.key, None)
    else:
        connector.mod_entry(action.table, action.key, {action.field: action.desired})


def _apply_actions(config_db, actions):
    connector = ValidatedConfigDBConnector(config_db)
    changed = [action for action in actions if action.changed]
    for action in changed:
        _write_action(connector, action)
    if any(action.table == "FRR_DAEMON" for action in changed):
        common.restart_bgp()
    common.verify_actions(config_db, actions)


def _persist(metadata):
    common.save_config()
    saved_drift = calculate_drift(metadata, common.read_saved_tables())
    if saved_drift:
        raise RuntimeError("saved SONiC configuration does not contain the verified persona intent")
    persisted = copy.deepcopy(metadata)
    persisted["persisted"] = True
    atomic_write_json(common.PERSISTENT_METADATA_PATH, persisted)
    atomic_write_json(common.LIVE_METADATA_PATH, persisted)
    return persisted


@click.group(cls=AbbreviationGroup, name="personaforge", invoke_without_command=False)
def personaforge():
    """Apply and persist PersonaForge profiles."""


@personaforge.command("apply")
@click.argument("profile")
@click.option("--persist", is_flag=True, help="Save verified intent for node reboot.")
@click.option("--allow-disruptive", is_flag=True, help="Authorize the required BGP restart.")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt for confirmation.")
@pass_db
def apply(db, profile, persist, allow_disruptive, yes):
    """Apply a profile to live ConfigDB."""
    try:
        plan = common.load_plan(profile, db.cfgdb)
        changed = [action for action in plan.actions if action.changed]
        if persist and not plan.persist_runtime:
            raise RuntimeContractError("profile policy does not authorize runtime persistence")
        if any(action.table == "FRR_DAEMON" for action in changed) and not allow_disruptive:
            raise RuntimeContractError("BGP restart requires --allow-disruptive")
        if changed and not yes:
            click.confirm("Apply persona '{}' to live ConfigDB?".format(plan.profile), abort=True)
        existing, _ = read_metadata((common.LIVE_METADATA_PATH, common.PERSISTENT_METADATA_PATH))
        if existing and existing.get("status") == "active":
            if existing.get("manifestSha256") != plan.manifest_sha256:
                raise RuntimeContractError(
                    "persona '{}' is already active; deactivate it before applying '{}'".format(
                        existing.get("profile", "unknown"), plan.profile
                    )
                )
            if calculate_drift(existing, common.config_tables(db.cfgdb)):
                raise RuntimeContractError("active persona has ConfigDB drift; inspect 'show personaforge drift'")
            if persist and not existing.get("persisted"):
                existing = _persist(existing)
            click.echo("Persona '{}' is already active{}".format(
                plan.profile, " and persisted" if existing.get("persisted") else ""
            ))
            return
        metadata = create_active_metadata(plan, False, "applying")
        atomic_write_json(common.LIVE_METADATA_PATH, metadata)
        _apply_actions(db.cfgdb, plan.actions)
        metadata = create_active_metadata(plan, False, "active")
        atomic_write_json(common.LIVE_METADATA_PATH, metadata)
        if persist:
            metadata = _persist(metadata)
        click.echo("Applied persona '{}'{}".format(
            plan.profile, " and persisted it" if metadata["persisted"] else ""
        ))
    except (OSError, RuntimeError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))


@personaforge.command("persist")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt for confirmation.")
@pass_db
def persist(db, yes):
    """Persist the currently active live persona."""
    try:
        metadata, _ = read_metadata((common.LIVE_METADATA_PATH, common.PERSISTENT_METADATA_PATH))
        if not metadata or metadata.get("status") != "active":
            raise RuntimeContractError("no active live persona to persist")
        if not metadata.get("policy", {}).get("persistRuntime"):
            raise RuntimeContractError("profile policy does not authorize runtime persistence")
        drift = calculate_drift(metadata, common.config_tables(db.cfgdb))
        if drift:
            raise RuntimeContractError("active persona has ConfigDB drift; refusing to persist")
        if not yes:
            click.confirm("Persist persona '{}' to the startup configuration?".format(
                metadata["profile"]), abort=True)
        _persist(metadata)
        click.echo("Persisted persona '{}'".format(metadata["profile"]))
    except (OSError, RuntimeError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))


@personaforge.command("deactivate")
@click.option("--persist", is_flag=True, help="Save deactivation for node reboot.")
@click.option("--allow-disruptive", is_flag=True, help="Authorize the required BGP restart.")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt for confirmation.")
@pass_db
def deactivate(db, persist, allow_disruptive, yes):
    """Restore values captured before the active persona was applied."""
    try:
        metadata, _ = read_metadata((common.LIVE_METADATA_PATH, common.PERSISTENT_METADATA_PATH))
        if persist and metadata and not metadata.get("policy", {}).get("persistRuntime"):
            raise RuntimeContractError("profile policy does not authorize runtime persistence")
        if metadata and metadata.get("status") == "inactive" and persist:
            if not yes:
                click.confirm("Persist the current persona deactivation?", abort=True)
            common.save_config()
            try:
                common.PERSISTENT_METADATA_PATH.unlink()
            except FileNotFoundError:
                pass
            click.echo("Persisted persona deactivation")
            return
        if not metadata or metadata.get("status") != "active":
            raise RuntimeContractError("no active persona to deactivate")
        actions = inverse_actions(metadata)
        changed = [action for action in actions if action.changed]
        if any(action.table == "FRR_DAEMON" for action in changed) and not allow_disruptive:
            raise RuntimeContractError("BGP restart requires --allow-disruptive")
        if changed and not yes:
            click.confirm("Deactivate persona '{}'?".format(metadata["profile"]), abort=True)
        _apply_actions(db.cfgdb, actions)
        inactive = copy.deepcopy(metadata)
        inactive.update({"status": "inactive", "persisted": False, "actions": []})
        atomic_write_json(common.LIVE_METADATA_PATH, inactive)
        if persist:
            common.save_config()
            try:
                common.PERSISTENT_METADATA_PATH.unlink()
            except FileNotFoundError:
                pass
        click.echo("Deactivated persona '{}'{}".format(
            metadata["profile"], " and persisted it" if persist else ""
        ))
    except (OSError, RuntimeError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))
