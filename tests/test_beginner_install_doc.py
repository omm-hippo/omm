"""docs/beginner-install.md quotes installer error strings with a
`<!-- file:line -->` citation. Keep every quote real and its citation
pointing at the line that prints it."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "beginner-install.md"
_QUOTE_AND_CITATION = re.compile(r"`([^`]+)`\s*<!--\s*([^>]+?)\s*-->")


def _citations():
    text = DOC.read_text(encoding="utf-8")
    for match in _QUOTE_AND_CITATION.finditer(text):
        quote = match.group(1).split("...")[0]
        for ref in match.group(2).split(","):
            filename, line = ref.strip().split(":")
            yield quote, filename, int(line)


def test_beginner_install_doc_cites_real_installer_lines():
    citations = list(_citations())
    assert len(citations) >= 15
    for quote, filename, line in citations:
        lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
        assert quote in lines[line - 1], f"{filename}:{line} no longer prints {quote!r}"


def test_beginner_install_doc_covers_required_topics():
    text = DOC.read_text(encoding="utf-8")
    for needle in (
        "omm web --open",
        "omm web --create-launchers",
        "omm setting web-autostart enable",
        "uninstall.ps1",
        "uninstall.sh",
        "OMM_HOME",
        "omm engine install",
        "새 창",
        "미검증",
    ):
        assert needle in text
