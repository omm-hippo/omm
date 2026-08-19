"""Regression guard: every text-mode subprocess call must pin ``encoding=``.

Without an explicit ``encoding``, Python decodes a child's piped output with
the interpreter's locale encoding - cp949 on a Korean Windows box, cp1252 on a
German one. The tools omm shells out to (winget, brew, flatpak, git, ollama,
lms, pipx) all write UTF-8 to a pipe, so the two ends disagree and the first
non-ASCII byte raises ``UnicodeDecodeError`` from deep inside the read, killing
the command mid-run.

That has now happened three times in this repo:

* the ``cli.py`` ``read_text`` crash fixed during the #100-era CI work,
* a benchmark subprocess fixed in #105,
* and PR #127, where ``omm setup`` died partway through installing an engine
  because winget printed a localized ``찾음`` line.

Each fix patched the site that happened to blow up. This test makes the *class*
unrepresentable: any new text-mode subprocess call without an explicit
``encoding=`` fails CI at the moment it is written, instead of on a user's
machine in a locale no maintainer runs.

Deliberate exceptions get a ``# noqa: subprocess-encoding`` comment on the line
of the call (or on the line the call starts on) together with a reason. There
are none today, and a new one should be argued for in review rather than added
quietly.
"""

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Callables that spawn a child process and can hand back decoded ``str``.
_SPAWNERS = frozenset(
    {
        "run",
        "call",
        "check_call",
        "check_output",
        "Popen",
        "getoutput",
        "getstatusoutput",
    }
)

# ``subprocess.getoutput``/``getstatusoutput`` decode with the locale encoding
# and take no ``encoding`` argument at all, so they are banned outright.
_ALWAYS_BANNED = frozenset({"getoutput", "getstatusoutput"})

_ALLOW_MARKER = "noqa: subprocess-encoding"


def _scanned_files():
    """Every first-party Python file this guard covers."""
    files = sorted((_REPO_ROOT / "src").rglob("*.py"))
    files += sorted((_REPO_ROOT / "scripts").glob("*.py"))
    return files


def _kwarg(call: ast.Call, name: str):
    """The keyword's value node, or None if the call does not pass it."""
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _is_truthy(node) -> bool:
    """True unless the node is a literal falsey constant.

    A non-literal (``text=want_text``) counts as possibly-text: the guard
    should err towards demanding an encoding rather than towards silence.
    """
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return bool(node.value)
    return True


def _subprocess_aliases(tree: ast.Module) -> dict:
    """Bare names bound to a subprocess spawner by ``from subprocess import``.

    Maps the local name to the canonical spawner name, so
    ``from subprocess import run as sp_run`` is still caught.
    """
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWNERS:
                    aliases[alias.asname or alias.name] = alias.name
    return aliases


def _spawner_name(call: ast.Call, aliases: dict):
    """Canonical spawner name for this callee, or None if it isn't one.

    Matches ``subprocess.run(...)`` (attribute on the module) and bare names
    imported from subprocess. A ``run`` attribute on anything else - say
    ``runner.run(..., text=True)`` - is deliberately not this guard's business.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            if func.attr in _SPAWNERS:
                return f"subprocess.{func.attr}", func.attr
        return None, None
    if isinstance(func, ast.Name) and func.id in aliases:
        return func.id, aliases[func.id]
    return None, None


def _covered_lines(call: ast.Call) -> range:
    """Lines an allowlist marker may legally sit on for this call."""
    return range(call.lineno, (call.end_lineno or call.lineno) + 1)


def _violations(path: Path):
    """Yield ``(lineno, callee, reason)`` for each unguarded call in ``path``."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=str(path))
    aliases = _subprocess_aliases(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee, attr = _spawner_name(node, aliases)
        if attr is None:
            continue

        if any(_ALLOW_MARKER in lines[i - 1] for i in _covered_lines(node) if i <= len(lines)):
            continue

        encoding = _kwarg(node, "encoding")

        if attr in _ALWAYS_BANNED:
            yield node.lineno, callee, (
                f"{callee} always decodes with the locale encoding and cannot "
                "be given an encoding; use subprocess.run(..., encoding=...)"
            )
            continue

        text_mode = _is_truthy(_kwarg(node, "text")) or _is_truthy(
            _kwarg(node, "universal_newlines")
        )
        if not text_mode and encoding is None:
            continue  # bytes mode: the caller decodes explicitly, or not at all
        if encoding is None:
            yield node.lineno, callee, (
                "text-mode subprocess call without an explicit encoding= - it "
                "will decode the child's output with the locale encoding "
                "(cp949 on Korean Windows) and crash on the first non-ASCII byte"
            )
            continue
        if isinstance(encoding, ast.Constant) and _kwarg(node, "errors") is None:
            yield node.lineno, callee, (
                "encoding= is pinned but errors= is not - an undecodable byte "
                'still raises; pass errors="replace"'
            )


def test_no_text_mode_subprocess_without_encoding():
    offenders = []
    for path in _scanned_files():
        for lineno, callee, reason in _violations(path):
            relpath = path.relative_to(_REPO_ROOT).as_posix()
            offenders.append(f"{relpath}:{lineno}: {callee}: {reason}")
    assert not offenders, (
        "Text-mode subprocess calls must pin the decoding explicitly. Add\n"
        '    encoding="utf-8", errors="replace"\n'
        "to each call below (or, for a deliberate exception, a "
        f"`# {_ALLOW_MARKER}: <reason>` comment on the call):\n"
        + "\n".join(offenders)
    )


def test_guard_covers_the_files_it_claims_to():
    """A silently empty scan would make this whole module vacuously pass."""
    scanned = _scanned_files()
    names = {p.name for p in scanned}
    assert len(scanned) > 20, f"guard scanned only {len(scanned)} files"
    # The three modules that actually shell out; if any is renamed away the
    # guard should be pointed at its replacement rather than quietly shrink.
    assert {"linker.py", "cli.py", "hardware.py"} <= names


@pytest.mark.parametrize(
    "snippet, expected",
    [
        ('subprocess.run(["git"], text=True)', True),
        ('subprocess.run(["git"], universal_newlines=True)', True),
        ('subprocess.run(["git"], text=True, encoding="utf-8")', True),  # no errors=
        ('subprocess.check_output(["git"], text=True)', True),
        ("subprocess.getoutput('git')", True),
        ('subprocess.Popen(["git"], stdout=PIPE, text=True)', True),
        ('subprocess.run(["git"], encoding="utf-8")', True),  # encoding implies text
        # Compliant / out of scope:
        ('subprocess.run(["git"], text=True, encoding="utf-8", errors="replace")', False),
        ('subprocess.run(["git"], capture_output=True)', False),  # bytes mode
        ('subprocess.run(["git"], text=False)', False),
        ('subprocess.run(["git"], text=True)  # noqa: subprocess-encoding: why', False),
        ('runner.run(["git"], text=True)', False),  # not the subprocess module
    ],
)
def test_guard_detects_violations(tmp_path, snippet, expected):
    """The guard must actually fire - an always-passing guard is worse than none.

    Mirrors how the module-scope-imports guard was validated by hand, but keeps
    the offending code in a temp file so the check is repeatable in CI instead
    of relying on someone remembering to break the tree once.
    """
    probe = tmp_path / "probe.py"
    probe.write_text(f"import subprocess\n{snippet}\n", encoding="utf-8")
    assert bool(list(_violations(probe))) is expected


def test_real_tree_is_clean_for_the_known_offender_files():
    """The sites from the #127 incident and this PR stay pinned.

    Named explicitly so that deleting the loop above (or narrowing
    ``_scanned_files``) cannot make the regression silently untested.
    """
    for relpath in (
        "src/omm/linker.py",
        "src/omm/cli.py",
        "src/omm/hardware.py",
        "src/omm/trust/__init__.py",
    ):
        path = _REPO_ROOT / relpath
        assert not list(_violations(path)), f"{relpath} regressed"
