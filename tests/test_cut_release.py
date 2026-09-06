"""Safety checks for scripts/cut_release.py.

The script's whole job is to refuse an unsafe release cut, so the tests drive
a real throwaway git repo (a bare ``origin`` plus a working clone) through
each refusal path rather than mocking git.
"""

from __future__ import annotations

import subprocess

import pytest

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "cut_release", Path(__file__).resolve().parents[1] / "scripts" / "cut_release.py"
)
cut_release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cut_release)


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t.test",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": __import__("os").environ["PATH"],
            "HOME": str(cwd),
        },
    )


def _pyproject(version: str) -> str:
    return f'[build-system]\nrequires = ["hatchling"]\n\n[project]\nname = "omm-model"\nversion = "{version}"\n'


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A working clone whose ``origin`` is a bare repo with ``main`` and
    ``beta``. ``beta`` is two commits ahead of ``main``."""
    origin = tmp_path / "origin.git"
    _run("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)

    work = tmp_path / "work"
    _run("clone", str(origin), str(work), cwd=tmp_path)
    (work / "pyproject.toml").write_text(_pyproject("0.3.10"))
    _run("add", "-A", cwd=work)
    _run("commit", "-m", "base", cwd=work)
    _run("push", "origin", "main", cwd=work)
    _run("checkout", "-b", "beta", cwd=work)
    (work / "a.txt").write_text("a")
    _run("add", "-A", cwd=work)
    _run("commit", "-m", "beta feature", cwd=work)
    (work / "pyproject.toml").write_text(_pyproject("0.3.11"))
    _run("add", "-A", cwd=work)
    _run("commit", "-m", "bump", cwd=work)
    _run("push", "origin", "beta", cwd=work)

    monkeypatch.setattr(cut_release, "ROOT", work)
    return work


def test_plan_release_accepts_the_beta_tip(repo):
    target, version = cut_release.plan_release(None, "origin")
    assert version == "0.3.11"
    assert target == subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/beta"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_plan_release_rejects_a_commit_not_on_beta(repo):
    _run("checkout", "-b", "rogue", "origin/main", cwd=repo)
    (repo / "x.txt").write_text("x")
    _run("add", "-A", cwd=repo)
    _run("commit", "-m", "not on beta", cwd=repo)
    rogue = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "rogue"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    with pytest.raises(cut_release.ReleaseCutError, match="not contained in origin/beta"):
        cut_release.plan_release(rogue, "origin")


def test_plan_release_rejects_a_non_fast_forward(repo, tmp_path):
    # Advance origin/main to a commit beta does not contain.
    other = tmp_path / "other"
    _run("clone", str(tmp_path / "origin.git"), str(other), cwd=tmp_path)
    (other / "diverge.txt").write_text("d")
    _run("add", "-A", cwd=other)
    _run("commit", "-m", "main-only", cwd=other)
    _run("push", "origin", "HEAD:main", cwd=other)
    with pytest.raises(cut_release.ReleaseCutError, match="fast-forward"):
        cut_release.plan_release(None, "origin")


def test_plan_release_rejects_when_main_already_there(repo):
    _run("push", "origin", "origin/beta:main", cwd=repo)
    with pytest.raises(cut_release.ReleaseCutError, match="already at|nothing to release"):
        cut_release.plan_release(None, "origin")


def test_plan_release_rejects_an_existing_tag(repo):
    _run("tag", "v0.3.11", "origin/beta", cwd=repo)
    _run("push", "origin", "v0.3.11", cwd=repo)
    _run("tag", "-d", "v0.3.11", cwd=repo)
    with pytest.raises(cut_release.ReleaseCutError, match="already exists on origin"):
        cut_release.plan_release(None, "origin")


def test_plan_release_rejects_a_dirty_tree(repo):
    (repo / "dirty.txt").write_text("dirty")
    with pytest.raises(cut_release.ReleaseCutError, match="not clean"):
        cut_release.plan_release(None, "origin")


def test_dry_run_writes_nothing(repo, capsys):
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert cut_release.cut_release(None, "origin", dry_run=True, assume_yes=True) == 0
    after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "origin/main"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert before == after
    assert "dry-run" in capsys.readouterr().out
