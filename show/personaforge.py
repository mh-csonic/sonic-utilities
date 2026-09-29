import json

import click
from tabulate import tabulate

from personaforge import PersonaForgeContractError, RuntimeContractError, calculate_drift, read_metadata
from utilities_common.cli import AbbreviationGroup, pass_db
from utilities_common import personaforge as common


def _emit(document, json_output):
    if json_output:
        click.echo(json.dumps(document, sort_keys=True, indent=2))


@click.group(cls=AbbreviationGroup, name="personaforge", invoke_without_command=False)
def personaforge():
    """Show PersonaForge plan and state."""


@personaforge.command("status")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@click.option("--detail", is_flag=True, help="Show desired ConfigDB entries.")
@pass_db
def status(db, json_output, detail):
    """Show active identity, persistence and ConfigDB convergence."""
    try:
        metadata, source = read_metadata((common.LIVE_METADATA_PATH, common.PERSISTENT_METADATA_PATH))
        if not metadata:
            document = {"status": "inactive", "persisted": False, "drift": []}
        else:
            document = dict(metadata)
            document["metadataSource"] = str(source)
            document["drift"] = calculate_drift(metadata, common.config_tables(db.cfgdb))
            saved_drift = calculate_drift(metadata, common.read_saved_tables())
            document["savedConfigMatches"] = not saved_drift
        if json_output:
            _emit(document, True)
            return
        rows = [
            ["Status", document.get("status", "unknown")],
            ["Profile", document.get("profile", "-")],
            ["Manifest", document.get("manifestSha256", "-")],
            ["Persisted", "yes" if document.get("persisted") else "no"],
            ["ConfigDB drift", len(document.get("drift", []))],
            ["Saved config matches", "yes" if document.get("savedConfigMatches") else "no"],
        ]
        click.echo(tabulate(rows, tablefmt="plain"))
        if detail and document.get("actions"):
            action_rows = [[
                "{}|{}".format(item["table"], item["key"]), item["field"], item["desired"]
            ] for item in document["actions"]]
            click.echo(tabulate(action_rows, ["Object", "Field", "Desired"]))
    except (OSError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))


@personaforge.command("plan")
@click.argument("profile")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@pass_db
def plan(db, profile, json_output):
    """Resolve a profile without mutating the system."""
    try:
        resolved = common.load_plan(profile, db.cfgdb)
        document = resolved.as_dict()
        if json_output:
            _emit(document, True)
            return
        rows = [[
            "{}|{}".format(action.table, action.key), action.field,
            action.previous if action.previous is not None else "<absent>",
            action.desired if action.desired is not None else "<absent>",
            "yes" if action.changed else "no",
        ] for action in resolved.actions]
        click.echo(tabulate(rows, ["Object", "Field", "Current", "Desired", "Change"]))
    except (OSError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))


@personaforge.command("drift")
@click.option("--json", "json_output", is_flag=True, help="Emit JSON.")
@pass_db
def drift(db, json_output):
    """Compare active desired state with live ConfigDB."""
    try:
        metadata, _ = read_metadata((common.LIVE_METADATA_PATH, common.PERSISTENT_METADATA_PATH))
        if not metadata or metadata.get("status") != "active":
            raise RuntimeContractError("no active persona")
        differences = calculate_drift(metadata, common.config_tables(db.cfgdb))
        document = {"profile": metadata["profile"], "drift": differences}
        if json_output:
            _emit(document, True)
            return
        if not differences:
            click.echo("No drift detected")
            return
        rows = [[
            "{}|{}".format(item["table"], item["key"]), item["field"],
            item["desired"], item["actual"] if item["actual"] is not None else "<absent>",
        ] for item in differences]
        click.echo(tabulate(rows, ["Object", "Field", "Desired", "Actual"]))
    except (OSError, PersonaForgeContractError, ValueError) as exc:
        raise click.ClickException(str(exc))
