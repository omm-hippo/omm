from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _covered(dockerignore_lines: list[str], gitignored_path: str) -> bool:
    target = gitignored_path.rstrip("/")
    for line in dockerignore_lines:
        prefix = line.rstrip("/")
        if target == prefix or target.startswith(prefix + "/"):
            return True
    return False


def test_dockerignore_covers_every_gitignored_secret_path():
    lines = _lines(ROOT / ".dockerignore")

    for gitignored_path in (
        ".claude/",
        ".firebase/",
        "no_upload/",
        "cf-worker/.dev.vars",
        "cf-worker/node_modules/",
        "cf-worker/.wrangler/",
        "demo/",
        ".worktrees/",
    ):
        assert _covered(lines, gitignored_path), gitignored_path

    assert ".git" in lines
    assert "**/__pycache__/" in lines


def test_dockerignore_keeps_what_the_image_needs():
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for required in (
        "src",
        "tests",
        "scripts",
        "published",
        "pyproject.toml",
        "requirements-train.txt",
    ):
        assert required not in text.splitlines()
