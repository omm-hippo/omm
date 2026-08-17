"""Regression guard: cli.py and onboarding.py must route all color
through omm.theme's semantic roles, never a literal color name, so a
future PR can't silently reintroduce a hardcoded-terminal-color bug."""

import re
from pathlib import Path

_LITERAL_MARKUP = re.compile(
    r"\[/?(?:bold )?(?:red|green|yellow|blue|cyan)\]"
)
_LITERAL_STYLE_KWARG = re.compile(
    r'style="(?:red|green|yellow|blue|cyan|white|dim)"'
)
_TARGET_FILES = ("src/omm/cli.py", "src/omm/onboarding.py")


def test_no_literal_color_markup_or_style_kwargs():
    repo_root = Path(__file__).resolve().parent.parent
    offenders = []
    for relpath in _TARGET_FILES:
        text = (repo_root / relpath).read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _LITERAL_MARKUP.search(line) or _LITERAL_STYLE_KWARG.search(line):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")
    assert not offenders, "Literal colors found - use a theme role instead:\n" + "\n".join(offenders)
