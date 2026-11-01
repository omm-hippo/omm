"""`omm rag mcp|connect|disconnect|share|unshare|team|remote`.

Agent bridge and company sharing commands for document search. Kept out of
cli.py so the document-search core and these can grow independently; cli.py
only calls :func:`register` on its `rag` group.
"""
from __future__ import annotations

import typer

_HARNESS_HELP = "claude-code, codex, pi or cursor."


def _fail(message: str) -> None:
    from omm.cli import err_console
    from rich.markup import escape
    err_console.print(f"[error]{escape(message)}[/error]")
    raise typer.Exit(1)


def mcp() -> None:
    """Run the local document search MCP server on stdin/stdout (started by a coding agent)."""
    from omm import rag_mcp
    raise typer.Exit(rag_mcp.serve())


def connect(
    harness: str = typer.Argument(..., help=_HARNESS_HELP),
    remote: bool = typer.Option(False, "--remote", help="Register the cluster head's shared document search (set with `omm rag remote set`) instead of this PC's."),
) -> None:
    """Register OMM document search as an MCP server in a coding agent, backing up its settings."""
    from omm import rag_connect
    from omm.cli import console
    from rich.markup import escape
    try:
        result = rag_connect.connect(harness, remote=remote)
    except (rag_connect.RagConnectionError, OSError) as error:
        _fail(str(error) if isinstance(error, rag_connect.RagConnectionError) else "Could not access configuration files. Check permissions.")
    if result["state"] == "unchanged":
        console.print(f"{escape(harness)} already has the OMM document search MCP server ({escape(result['path'])}).")
        return
    console.print(f"Registered MCP server '{rag_connect.SERVER_NAME}' for {escape(harness)} in {escape(result['path'])}.")
    if remote:
        console.print("It searches the cluster head over the office network. Your employee token is stored in that "
                      "configuration file; do not share or upload the file.")
        from omm import rag_remote
        from omm.cluster import tls
        hint, command = tls.harness_trust_hint(harness, rag_remote.ca_path())
        console.print(escape(hint))
        console.print(f"  {command}", markup=False, soft_wrap=True)
        console.print("Start a new session in the harness.")
    else:
        console.print("Start a new session in the harness. Documents and questions stay on this computer.")
    if harness == "claude-code":
        console.print("If Claude Code is running, restart it so it does not overwrite the new entry.")


def disconnect(harness: str = typer.Argument(..., help=_HARNESS_HELP)) -> None:
    """Remove only the OMM MCP entry from a coding agent and keep other settings."""
    from omm import rag_connect
    from omm.cli import console
    try:
        result = rag_connect.disconnect(harness)
    except (rag_connect.RagConnectionError, OSError) as error:
        _fail(str(error) if isinstance(error, rag_connect.RagConnectionError) else "Could not access configuration files. Backup retained.")
    console.print({
        "restored": "Original settings restored.",
        "removed": "Removed the OMM MCP entry; other settings were kept.",
        "absent": "The OMM MCP entry was already gone; nothing changed.",
        "not_connected": "OMM did not register an MCP server for this harness; nothing changed.",
    }[result["state"]])


# -- sharing (#504) ---------------------------------------------------------

def _printers():
    from omm.cli import console
    from rich.markup import escape
    return console, escape


def remote_client():
    """The head client when `omm rag remote set` is active, else None (this PC's store)."""
    from omm import rag_remote
    try:
        return rag_remote.client()
    except rag_remote.RemoteError as error:
        _fail(str(error))


def _target_label(kind: str, target: str) -> str:
    return f"team {target}" if kind == "team" else target


def share(
    collection: str = typer.Argument(..., help="Collection name."),
    person: str | None = typer.Argument(None, help="Employee token name (see `omm cluster token list` on the head)."),
    team: str | None = typer.Option(None, "--team", help="Share with a team instead of one person."),
) -> None:
    """Let an employee or a team search a collection (its owner or the head administrator)."""
    from omm import rag_acl
    console, escape = _printers()
    client = remote_client()
    try:
        result = (client.share(collection, person=person, team=team) if client
                  else rag_acl.share(rag_acl.ADMIN, collection, person=person, team=team))
    except ValueError as error:
        _fail(str(error))
    label = _target_label(result["kind"], result["target"])
    if result["added"]:
        console.print(f"Shared collection '{escape(result['collection'])}' with {escape(label)}. They can search it now.")
    else:
        console.print(f"Collection '{escape(result['collection'])}' is already shared with {escape(label)}.")


def unshare(
    collection: str = typer.Argument(..., help="Collection name."),
    person: str | None = typer.Argument(None, help="Employee token name."),
    team: str | None = typer.Option(None, "--team", help="Withdraw a team share instead of a person's."),
) -> None:
    """Stop sharing a collection with an employee or a team (its owner or the head administrator)."""
    from omm import rag_acl
    console, escape = _printers()
    client = remote_client()
    try:
        result = (client.unshare(collection, person=person, team=team) if client
                  else rag_acl.unshare(rag_acl.ADMIN, collection, person=person, team=team))
    except ValueError as error:
        _fail(str(error))
    label = _target_label(result["kind"], result["target"])
    if result["removed"]:
        console.print(f"Stopped sharing collection '{escape(result['collection'])}' with {escape(label)}.")
    else:
        console.print(f"Collection '{escape(result['collection'])}' was not shared with {escape(label)}; nothing changed.")


team_app = typer.Typer(name="team", help="Manage employee teams for sharing collections (on the cluster head).",
                       rich_markup_mode=None)


def _team_call(function, *args):
    from omm import rag_remote
    if rag_remote.remote_path().exists():
        _fail("Teams are managed on the head Mac only. This PC is set to use a remote head (`omm rag remote`).")
    try:
        return function(*args)
    except ValueError as error:
        _fail(str(error))


@team_app.command(name="create")
def team_create(name: str = typer.Argument(..., help="Team name, e.g. sales.")) -> None:
    """Create an empty team."""
    from omm import rag_acl
    console, escape = _printers()
    _team_call(rag_acl.team_create, name)
    console.print(f"Created team '{escape(name)}'. Add people with `omm rag team add {escape(name)} <employee token name>`.")


@team_app.command(name="delete")
def team_delete(name: str = typer.Argument(..., help="Team to delete.")) -> None:
    """Delete a team and every collection share made to it."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_delete, name)
    console.print(f"Deleted team '{escape(name)}' and withdrew its {result['shares_removed']} share(s).")


@team_app.command(name="add")
def team_add(team: str = typer.Argument(..., help="Team name."),
             member: str = typer.Argument(..., help="Employee token name.")) -> None:
    """Add an employee (token name) to a team."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_add, team, member)
    done = "Added" if result["added"] else "Already added"
    console.print(f"{done} {escape(member)} to team '{escape(team)}'.")


@team_app.command(name="remove")
def team_remove(team: str = typer.Argument(..., help="Team name."),
                member: str = typer.Argument(..., help="Employee token name.")) -> None:
    """Remove an employee from a team."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_remove, team, member)
    if result["removed"]:
        console.print(f"Removed {escape(member)} from team '{escape(team)}'.")
    else:
        console.print(f"{escape(member)} is not in team '{escape(team)}'; nothing changed.")


@team_app.command(name="list")
def team_list() -> None:
    """List teams and their members."""
    from omm import rag_acl
    console, escape = _printers()
    rows = _team_call(rag_acl.teams)
    if not rows:
        console.print("No teams yet. Create one with `omm rag team create <team name>`.")
    for row in rows:
        members = ", ".join(row["members"]) or "(no members)"
        console.print(f"{escape(row['name'])}: {escape(members)}")


remote_app = typer.Typer(name="remote", help="Use shared collections on the cluster head from this PC.",
                         rich_markup_mode=None, invoke_without_command=True)


@remote_app.callback()
def remote_show(ctx: typer.Context) -> None:
    """Show which cluster head `omm rag` uses (the token is never printed)."""
    if ctx.invoked_subcommand is not None:
        return
    from omm import rag_remote
    console, escape = _printers()
    try:
        remote = rag_remote.load()
    except rag_remote.RemoteError as error:
        _fail(str(error))
    if remote is None:
        console.print("No remote head is set. `omm rag` commands use this PC's documents.")
        return
    console.print(f"Remote head: {escape(remote.url)} (token saved)")
    from omm import rag_readiness
    console.print(escape(rag_readiness.remote_line_en(rag_readiness.remote_view(rag_remote.Client(remote)))))
    console.print("The collection, add, remove, search, share and unshare commands use the head's documents. "
                  "Run `omm rag remote clear` to go back.")


def _read_token(token: str | None) -> str:
    if token:
        return token.strip()
    import sys
    from omm.cli import _stdin_is_tty
    if _stdin_is_tty():
        token = typer.prompt("Employee token (input is hidden)", hide_input=True)
    else:
        token = sys.stdin.readline()
    return (token or "").strip()


def _trust_head_certificate(offered, url: str, yes: bool) -> None:
    """Pin the head's certificate on first use; refuse one that differs from the pinned one.

    A different certificate at another address than the saved one is a new
    head (after `omm cluster handover` it has its own CA), so it is shown and
    asked about like a first use instead of refused."""
    from omm import rag_remote
    from omm.cluster import tls
    console, escape = _printers()
    pinned = rag_remote.pinned_ca()
    if pinned:
        try:
            same = tls.fingerprint(pinned) == offered.fingerprint
        except ValueError:
            same = False
        if same:
            return
        saved = rag_remote.load()
        if saved is None or saved.url == url:
            raise rag_remote.RemoteError(rag_remote.wrong_certificate())
        console.print(f"This is a new head address (was {escape(saved.url)}). Checking the new head's certificate.")
    console.print("Head certificate fingerprint (SHA-256):")
    console.print(f"  {offered.fingerprint}", markup=False, soft_wrap=True, emoji=False)
    console.print("Check that it matches the fingerprint `omm cluster status` shows on the head Mac "
                  "(the one you received with your token).")
    if yes:
        return
    from omm.cli import _ask_confirm, _stdin_is_tty
    if not _stdin_is_tty():
        raise rag_remote.RemoteError("Not a terminal, so the fingerprint cannot be confirmed here. "
                                     "If you checked that it matches, run again with --yes.")
    if not _ask_confirm("Does the fingerprint match? Trust this head?"):
        raise rag_remote.RemoteError("The head certificate was not trusted; nothing was saved.")


@remote_app.command(name="set")
def remote_set(
    url: str = typer.Argument(..., help="Cluster head address, e.g. 192.168.0.10 or https://head.local:52440."),
    token: str | None = typer.Option(None, "--token", help="Employee token. Omit it to type the token hidden (or pipe it on stdin) so it stays out of shell history."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Trust the head's certificate without asking (needed when the token comes on stdin). Compare the printed fingerprint with omm cluster status on the head yourself."),
) -> None:
    """Point `omm rag` at the cluster head after checking its certificate and the employee token."""
    from omm import rag_remote
    from omm.cluster import tls
    console, escape = _printers()
    try:
        remote = rag_remote.Remote(rag_remote.normalize_url(url), _read_token(token))
        if not remote.token:
            raise rag_remote.RemoteError("The token is empty. Enter the omm_... token from your administrator.")
        # The certificate is settled before the token is sent anywhere.
        offered = tls.fetch_ca(remote.url)
        _trust_head_certificate(offered, remote.url, yes)
        name = rag_remote.Client(remote, ca_pem=offered.pem).whoami()
        rag_remote.save_ca(offered.pem)
        path = rag_remote.save(remote)
    except (ValueError, OSError) as error:
        _fail(str(error) if isinstance(error, ValueError) else "Could not write the settings file. Check permissions.")
    console.print(f"Connected to the head {escape(remote.url)} as '{escape(name)}'. The token is stored only in {escape(str(path))}.")
    console.print("`omm rag collection list`, `omm rag add` and `omm rag search` now use the head's documents. "
                  "To connect an agent, run `omm rag connect <harness> --remote`.")


@remote_app.command(name="clear")
def remote_clear() -> None:
    """Forget the cluster head; `omm rag` uses this PC's documents again."""
    from omm import rag_remote
    console, _ = _printers()
    removed = rag_remote.clear()
    if not removed:
        console.print("No remote head was set; nothing changed.")
        return
    console.print("Removed the remote head settings and token. `omm rag` uses this PC's documents again.")
    console.print("If you connected an agent with `--remote`, run `omm rag disconnect <harness>` to remove the token from its settings too.")


def register(group: typer.Typer) -> None:
    # Imported here, not at module top: omm.cli imports this module.
    from omm.cli import _DocsEpilogGroup

    team_app.info.cls = _DocsEpilogGroup
    group.command(name="mcp")(mcp)
    group.command(name="connect")(connect)
    group.command(name="disconnect")(disconnect)
    group.command(name="share")(share)
    group.command(name="unshare")(unshare)
    group.add_typer(team_app)
    group.add_typer(remote_app)
