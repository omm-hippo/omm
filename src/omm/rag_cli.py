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
    return f"팀 {target}" if kind == "team" else target


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
        console.print(f"'{escape(result['collection'])}' 컬렉션을 {escape(label)}에게 공유했어요. 이제 검색할 수 있어요.")
    else:
        console.print(f"'{escape(result['collection'])}' 컬렉션은 이미 {escape(label)}에게 공유돼 있어요.")


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
        console.print(f"'{escape(result['collection'])}' 컬렉션 공유를 {escape(label)}에게서 거뒀어요.")
    else:
        console.print(f"'{escape(result['collection'])}' 컬렉션은 {escape(label)}에게 공유돼 있지 않았어요. 바뀐 것은 없어요.")


team_app = typer.Typer(name="team", help="Manage employee teams for sharing collections (on the cluster head).",
                       rich_markup_mode=None, no_args_is_help=True)


def _team_call(function, *args):
    from omm import rag_remote
    if rag_remote.remote_path().exists():
        _fail("팀은 헤드 Mac에서만 관리해요. 이 PC는 원격 헤드를 쓰도록 설정돼 있어요(`omm rag remote`).")
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
    console.print(f"'{escape(name)}' 팀을 만들었어요. `omm rag team add {escape(name)} <직원 토큰 이름>`으로 사람을 넣어 주세요.")


@team_app.command(name="delete")
def team_delete(name: str = typer.Argument(..., help="Team to delete.")) -> None:
    """Delete a team and every collection share made to it."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_delete, name)
    console.print(f"'{escape(name)}' 팀을 지웠어요. 이 팀에 준 공유 {result['shares_removed']}개도 거뒀어요.")


@team_app.command(name="add")
def team_add(team: str = typer.Argument(..., help="Team name."),
             member: str = typer.Argument(..., help="Employee token name.")) -> None:
    """Add an employee (token name) to a team."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_add, team, member)
    done = "넣었어요" if result["added"] else "이미 넣어 두었어요"
    console.print(f"{escape(member)}을(를) '{escape(team)}' 팀에 {done}.")


@team_app.command(name="remove")
def team_remove(team: str = typer.Argument(..., help="Team name."),
                member: str = typer.Argument(..., help="Employee token name.")) -> None:
    """Remove an employee from a team."""
    from omm import rag_acl
    console, escape = _printers()
    result = _team_call(rag_acl.team_remove, team, member)
    done = "뺐어요" if result["removed"] else "찾지 못했어요. 바뀐 것은 없어요"
    console.print(f"{escape(member)}을(를) '{escape(team)}' 팀에서 {done}.")


@team_app.command(name="list")
def team_list() -> None:
    """List teams and their members."""
    from omm import rag_acl
    console, escape = _printers()
    rows = _team_call(rag_acl.teams)
    if not rows:
        console.print("팀이 없어요. `omm rag team create <팀 이름>`으로 만들 수 있어요.")
    for row in rows:
        members = ", ".join(row["members"]) or "(사람 없음)"
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
        console.print("원격 헤드가 설정돼 있지 않아요. `omm rag` 명령은 이 PC의 문서를 써요.")
        return
    console.print(f"원격 헤드: {escape(remote.url)} (토큰 저장됨)")
    console.print("collection·add·remove·search·share·unshare 명령이 헤드의 문서를 써요. "
                  "되돌리려면 `omm rag remote clear`.")


def _read_token(token: str | None) -> str:
    if token:
        return token.strip()
    import sys
    from omm.cli import _stdin_is_tty
    if _stdin_is_tty():
        token = typer.prompt("직원 토큰 (입력이 보이지 않아요)", hide_input=True)
    else:
        token = sys.stdin.readline()
    return (token or "").strip()


def _trust_head_certificate(offered, yes: bool) -> None:
    """Pin the head's certificate on first use; refuse one that differs from the pinned one."""
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
        raise rag_remote.RemoteError(rag_remote.WRONG_CERTIFICATE)
    console.print("헤드 인증서 지문 (SHA-256):")
    console.print(f"  {offered.fingerprint}", markup=False, soft_wrap=True, emoji=False)
    console.print("헤드 Mac에서 `omm cluster status` 가 보여 주는 지문(토큰을 받을 때 함께 받은 지문)과 똑같은지 확인해 주세요.")
    if yes:
        return
    from omm.cli import _ask_confirm, _stdin_is_tty
    if not _stdin_is_tty():
        raise rag_remote.RemoteError("터미널이 아니라 지문을 확인받을 수 없어요. 지문이 같은 것을 직접 확인했다면 --yes 를 붙여 다시 실행해 주세요.")
    if not _ask_confirm("지문이 같나요? 이 헤드를 신뢰할까요?"):
        raise rag_remote.RemoteError("헤드 인증서를 신뢰하지 않아서 아무것도 저장하지 않았어요.")


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
            raise rag_remote.RemoteError("토큰이 비어 있어요. 관리자에게 받은 omm_... 토큰을 넣어 주세요.")
        # The certificate is settled before the token is sent anywhere.
        offered = tls.fetch_ca(remote.url)
        _trust_head_certificate(offered, yes)
        name = rag_remote.Client(remote, ca_pem=offered.pem).whoami()
        rag_remote.save_ca(offered.pem)
        path = rag_remote.save(remote)
    except (ValueError, OSError) as error:
        _fail(str(error) if isinstance(error, ValueError) else "설정 파일을 쓰지 못했어요. 권한을 확인해 주세요.")
    console.print(f"{escape(remote.url)} 헤드에 '{escape(name)}'(으)로 연결했어요. 토큰은 {escape(str(path))} 에만 저장했어요.")
    console.print("이제 `omm rag collection list`, `omm rag add`, `omm rag search` 가 헤드의 문서를 써요. "
                  "에이전트에 붙이려면 `omm rag connect <하네스> --remote`.")


@remote_app.command(name="clear")
def remote_clear() -> None:
    """Forget the cluster head; `omm rag` uses this PC's documents again."""
    from omm import rag_remote
    console, _ = _printers()
    removed = rag_remote.clear()
    if not removed:
        console.print("원격 헤드가 설정돼 있지 않았어요. 바뀐 것은 없어요.")
        return
    console.print("원격 헤드 설정과 토큰을 지웠어요. 이제 이 PC의 문서를 써요.")
    console.print("에이전트에 `--remote`로 연결해 두었다면 `omm rag disconnect <하네스>`로 그 설정의 토큰도 지워 주세요.")


def register(group: typer.Typer) -> None:
    group.command(name="mcp")(mcp)
    group.command(name="connect")(connect)
    group.command(name="disconnect")(disconnect)
    group.command(name="share")(share)
    group.command(name="unshare")(unshare)
    group.add_typer(team_app)
    group.add_typer(remote_app)
