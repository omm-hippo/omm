"""Regression guard: cli.py and onboarding.py must route all color
through omm.theme's semantic roles, never a literal color name, so a
future PR can't silently reintroduce a hardcoded-terminal-color bug."""

import re
from pathlib import Path

_LITERAL_MARKUP = re.compile(
    # `dim` belongs here as much as the five color names: the spec lists
    # dim -> muted among the migrated tokens, and `[dim]` is the single
    # most natural rich idiom for a future PR to reach for.
    r"\[/?(?:bold )?(?:red|green|yellow|blue|cyan|dim)\]"
)
_LITERAL_STYLE_KWARG = re.compile(
    r'style="(?:red|green|yellow|blue|cyan|white|dim)"'
)
_TARGET_FILES = ("src/omm/cli.py", "src/omm/onboarding.py")

# The one deliberate exception, matched by exact source text so that
# nothing else - not even another line in the same function - can slip
# through: the `omm setup` ASCII banner prints *before* the wizard's theme
# step, so it must not be rendered in a theme the user hasn't chosen yet
# (design spec, "Non-goals"). Editing either line here means editing this
# allowlist too, which is the point: it forces the exemption to be
# re-confirmed rather than silently widened.
_EXEMPT_LINES: dict[str, tuple[str, ...]] = {
    "src/omm/onboarding.py": (
        'console.print(f"[bold blue]{_ASCII_ART}[/bold blue]")',
        'console.print("[bold blue]omm[/bold blue] - local LLM package manager")',
    ),
}


def test_no_literal_color_markup_or_style_kwargs():
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for relpath in _TARGET_FILES:
        exempt = _EXEMPT_LINES.get(relpath, ())
        text = (repo_root / relpath).read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip() in exempt:
                continue
            if _LITERAL_MARKUP.search(line) or _LITERAL_STYLE_KWARG.search(line):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")
    assert not offenders, "Literal colors found - use a theme role instead:\n" + "\n".join(offenders)


def test_exempt_banner_lines_are_still_present_verbatim():
    """An allowlist entry that no longer matches any source line is a
    silently dead exemption - and would mean the banner had been re-themed
    (or reworded) without anyone revisiting the non-goal it encodes."""
    repo_root = Path(__file__).resolve().parent.parent
    for relpath, exempt in _EXEMPT_LINES.items():
        lines = {line.strip() for line in (repo_root / relpath).read_text().splitlines()}
        for entry in exempt:
            assert entry in lines, f"{relpath} no longer contains exempt line: {entry}"
