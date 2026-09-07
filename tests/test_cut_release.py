"""Safety checks for scripts/cut_release.py.

The script refuses an unsafe release cut, so the tests drive a real throwaway
git repo (a bare ``origin`` plus a working clone) through each path. The GitHub
side (`_gh`) is stubbed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cut_release", Path(__file__).resolve().parents[1] / "scripts" / "cut_release.py"
)
cut_release = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cut_release)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t.test",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t.test",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "PATH": os.environ["PATH"],
}


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**_GIT_ENV, "HOME": str(cwd)},
    )


def _pyproject(version: str) -> str:
    return (
        '[build-system]\nrequires = ["hatchling"]\n\n'
        f'[project]\nname = "omm-model"\nversion = "{version}"\n'
    )


def _launcher(version: str) -> str:
    return json.dumps(
        {
            "name": "@omm-hippo/omm",
            "version": version,
            "optionalDependencies": {
                "@omm-hippo/omm-darwin-arm64": version,
                "@omm-hippo/omm-linux-x64-gnu": version,
            },
        },
        indent=2,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Working clone whose ``origin`` has ``main`` and ``beta``; ``beta`` is two
    commits ahead. `_gh` is stubbed to report all required checks green."""
    origin = tmp_path / "origin.git"
    _run("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    work = tmp_path / "work"
    _run("clone", str(origin), str(work), cwd=tmp_path)

    for key, value in (
        ("user.name", "t"), ("user.email", "t@t.test"),
        ("commit.gpgsign", "false"), ("tag.gpgsign", "false"),
    ):
        _run("config", key, value, cwd=work)
    (work / "pyproject.toml").write_text(_pyproject("0.3.10"))
    (work / "packaging" / "npm" / "launcher").mkdir(parents=True)
    (work / "packaging" / "npm" / "launcher" / "package.json").write_text(_launcher("0.3.10"))
    _run("add", "-A", cwd=work)
    _run("commit", "-m", "base", cwd=work)
    _run("push", "origin", "main", cwd=work)
    _run("checkout", "-b", "beta", cwd=work)
    for name in ("a", "b"):
        (work / f"{name}.txt").write_text(name)
        _run("add", "-A", cwd=work)
        _run("commit", "-m", f"beta {name}", cwd=work)
    _run("push", "origin", "beta", cwd=work)

    monkeypatch.setattr(cut_release, "ROOT", work)
    monkeypatch.setattr(cut_release, "PYPROJECT", work / "pyproject.toml")
    monkeypatch.setattr(
        cut_release, "NPM_LAUNCHER", work / "packaging" / "npm" / "launcher" / "package.json"
    )
    monkeypatch.setattr(cut_release, "_repo_slug", lambda remote: "omm-hippo/omm")

    # No signing key in the test env; keep the release tag annotated-not-signed.
    _real_git = cut_release._git
    monkeypatch.setattr(
        cut_release,
        "_git",
        lambda *a, **kw: _real_git(*("-a" if x == "-s" else x for x in a), **kw),
    )

    def fake_gh(*args: str) -> str:
        joined = " ".join(args)
        if "branches/beta/protection" in joined:
            return json.dumps({"required_status_checks": {"contexts": ["ci", "trust"]}})
        if "check-runs" in joined:
            return json.dumps(
                {"check_runs": [{"name": "ci", "conclusion": "success"},
                                {"name": "trust", "conclusion": "success"}]}
            )
        raise AssertionError(args)

    monkeypatch.setattr(cut_release, "_gh", fake_gh)
    return work


def _rev(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_plan_bumps_the_patch_and_targets_beta_tip(repo):
    target, current, nxt = cut_release.plan_release("origin", skip_ci_check=False)
    assert (current, nxt) == ("0.3.10", "0.3.11")
    assert target == _rev(repo, "origin/beta")


def test_bump_patch_rejects_non_xyz():
    with pytest.raises(cut_release.ReleaseCutError, match="not a plain X.Y.Z"):
        cut_release._bump_patch("0.3.11.dev4")


def test_plan_rejects_when_main_already_at_beta(repo):
    _run("push", "origin", "origin/beta:main", cwd=repo)
    with pytest.raises(cut_release.ReleaseCutError, match="nothing to release"):
        cut_release.plan_release("origin", skip_ci_check=True)


def test_plan_rejects_diverged_branches(repo, tmp_path):
    other = tmp_path / "other"
    _run("clone", str(tmp_path / "origin.git"), str(other), cwd=tmp_path)
    (other / "diverge.txt").write_text("d")
    _run("add", "-A", cwd=other)
    _run("commit", "-m", "main-only", cwd=other)
    _run("push", "origin", "HEAD:main", cwd=other)
    with pytest.raises(cut_release.ReleaseCutError, match="not a fast-forward"):
        cut_release.plan_release("origin", skip_ci_check=True)


def test_plan_rejects_existing_tag(repo):
    _run("tag", "v0.3.11", "origin/beta", cwd=repo)
    _run("push", "origin", "v0.3.11", cwd=repo)
    _run("tag", "-d", "v0.3.11", cwd=repo)
    with pytest.raises(cut_release.ReleaseCutError, match="already exists on origin"):
        cut_release.plan_release("origin", skip_ci_check=True)


def test_plan_rejects_dirty_tree(repo):
    (repo / "dirty.txt").write_text("dirty")
    with pytest.raises(cut_release.ReleaseCutError, match="not clean"):
        cut_release.plan_release("origin", skip_ci_check=False)


def test_plan_rejects_a_red_required_check(repo, monkeypatch):
    def red_gh(*args: str) -> str:
        if "branches/beta/protection" in " ".join(args):
            return json.dumps({"required_status_checks": {"contexts": ["ci", "trust"]}})
        return json.dumps({"check_runs": [{"name": "ci", "conclusion": "failure"},
                                          {"name": "trust", "conclusion": "success"}]})

    monkeypatch.setattr(cut_release, "_gh", red_gh)
    with pytest.raises(cut_release.ReleaseCutError, match="required checks not green.*ci"):
        cut_release.plan_release("origin", skip_ci_check=False)


def test_skip_ci_check_bypasses_gh(repo, monkeypatch):
    monkeypatch.setattr(cut_release, "_gh", lambda *a: (_ for _ in ()).throw(AssertionError("gh called")))
    _, _, nxt = cut_release.plan_release("origin", skip_ci_check=True)
    assert nxt == "0.3.11"


def test_dry_run_writes_and_pushes_nothing(repo, capsys):
    before_beta, before_main = _rev(repo, "origin/beta"), _rev(repo, "origin/main")
    pyproject_before = (repo / "pyproject.toml").read_text()
    assert cut_release.cut_release("origin", dry_run=True, assume_yes=True, skip_ci_check=False) == 0
    assert _rev(repo, "origin/beta") == before_beta
    assert _rev(repo, "origin/main") == before_main
    assert (repo / "pyproject.toml").read_text() == pyproject_before
    out = capsys.readouterr().out
    assert "dry-run" in out and "0.3.10 -> 0.3.11" in out


def test_full_cut_bumps_pushes_beta_main_and_tags(repo):
    _run("checkout", "beta", cwd=repo)
    assert cut_release.cut_release("origin", dry_run=False, assume_yes=True, skip_ci_check=False) == 0

    _run("fetch", "origin", "--tags", cwd=repo)
    beta_tip, main_tip = _rev(repo, "origin/beta"), _rev(repo, "origin/main")
    assert beta_tip == main_tip
    assert _rev(repo, "v0.3.11^{commit}") == main_tip

    released = subprocess.run(
        ["git", "-C", str(repo), "show", f"{main_tip}:pyproject.toml"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert 'version = "0.3.11"' in released
    launcher = subprocess.run(
        ["git", "-C", str(repo), "show", f"{main_tip}:packaging/npm/launcher/package.json"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert launcher.count('"0.3.11"') == 3  # version + 2 optional deps
    assert subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s", main_tip],
        capture_output=True, text=True, check=True,
    ).stdout.strip() == "release: v0.3.11"


def test_full_cut_restores_the_callers_checkout(repo):
    _run("checkout", "beta", cwd=repo)
    cut_release.cut_release("origin", dry_run=False, assume_yes=True, skip_ci_check=True)
    assert _rev(repo, "HEAD") == _rev(repo, "beta")
    branch = subprocess.run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert branch == "beta"
