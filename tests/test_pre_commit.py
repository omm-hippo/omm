"""Exercise the installed version hook through real commits in a scratch repo."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path("packaging/npm/launcher/package.json")


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True,
        check=check, timeout=30,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


@pytest.fixture
def hook_repo(tmp_path, request):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    for name in ("pre-commit", "npm_package.py"):
        shutil.copy2(ROOT / "scripts" / name, repo / "scripts" / name)
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    if getattr(request, "param", "lf") == "crlf":
        project = repo / "pyproject.toml"
        project.write_bytes(project.read_bytes().replace(b"\n", b"\r\n"))
    shutil.copy2(ROOT / "LICENSE", repo / "LICENSE")
    shutil.copytree(ROOT / MANIFEST.parent, repo / MANIFEST.parent)
    (repo / "change.txt").write_text("initial\n")
    git(repo, "init")
    git(repo, "config", "user.name", "Hook test")
    git(repo, "config", "user.email", "hook-test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "scripts", "pyproject.toml", "LICENSE", "packaging", "change.txt")
    git(repo, "commit", "-m", "initial fixture")
    git(repo, "config", "core.hooksPath", "scripts")
    return repo


def project_version(repo):
    return re.search(r'^version = "([^"]+)"', (repo / "pyproject.toml").read_text(), re.M)[1]


def assert_committed_versions(repo, expected):
    assert project_version(repo) == expected
    manifest = json.loads(git(repo, "show", f"HEAD:{MANIFEST.as_posix()}").stdout)
    assert manifest["version"] == expected
    assert len(manifest["optionalDependencies"]) == 5
    assert set(manifest["optionalDependencies"].values()) == {expected}
    subprocess.run(
        [sys.executable, "scripts/npm_package.py", "validate"], cwd=repo,
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert git(repo, "status", "--porcelain").stdout == ""


@pytest.mark.parametrize("manual", [False, True, "launcher-already-updated"])
@pytest.mark.parametrize("hook_repo", ["lf", "crlf"], indirect=True)
def test_hook_keeps_committed_platform_versions_in_sync(hook_repo, manual):
    repo = hook_repo
    old = project_version(repo)
    major, minor, patch = map(int, old.split("."))
    expected = f"{major + 1}.0.0" if manual else f"{major}.{minor}.{patch + 1}"
    if manual:
        project = repo / "pyproject.toml"
        project.write_bytes(project.read_bytes().replace(
            f'version = "{old}"'.encode(), f'version = "{expected}"'.encode(), 1,
        ))
        git(repo, "add", "pyproject.toml")
    if manual == "launcher-already-updated":
        manifest = json.loads((repo / MANIFEST).read_text())
        manifest["version"] = expected
        (repo / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
        git(repo, "add", MANIFEST.as_posix())
    (repo / "change.txt").write_text("changed\n")
    git(repo, "add", "change.txt")
    git(repo, "commit", "-m", "exercise version hook")
    assert_committed_versions(repo, expected)


@pytest.mark.parametrize("metadata", [Path("pyproject.toml"), MANIFEST])
def test_hook_does_not_stage_unrelated_unstaged_metadata(hook_repo, metadata):
    repo = hook_repo
    path = repo / metadata
    path.write_text(path.read_text() + "\n")
    before = path.read_bytes()
    (repo / "change.txt").write_text("changed\n")
    git(repo, "add", "change.txt")
    result = git(repo, "commit", "-m", "keep separate metadata edits", check=False)
    assert result.returncode != 0
    assert "unstaged" in result.stderr.lower()
    assert path.read_bytes() == before
    assert git(repo, "diff", "--cached", "--name-only").stdout.splitlines() == ["change.txt"]
