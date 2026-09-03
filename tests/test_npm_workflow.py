import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_npm_workflow_is_validation_only_and_pinned():
    workflow_path = ROOT / ".github" / "workflows" / "npm-package.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert 'node: ["22", "24"]' in workflow
    for runner in (
        "ubuntu-24.04",
        "ubuntu-22.04-arm",
        "macos-26",
        "macos-15-intel",
        "windows-2025",
    ):
        assert runner in workflow
    assert "scripts/npm_package.py validate" in workflow
    for target in (
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64-gnu",
        "linux-x64-gnu",
    ):
        assert target in workflow
    assert "scripts/npm_binary.py build" in workflow
    assert "npm update --global @omm-hippo/omm" in workflow
    assert "npm uninstall --global" in workflow
    binary_section = workflow.split("\n  binary:\n", maxsplit=1)[1]
    assert "windows-" not in binary_section
    assert "win32-x64" not in binary_section
    assert "npm pack --dry-run --json" in workflow
    assert "npm publish" not in workflow
    assert "id-token: write" not in workflow
    assert "persist-credentials: false" in workflow
    assert not re.search(r"uses:\s+[^\s#]+@v\d", workflow)


def test_npm_release_workflow_is_gated_and_verifies_every_public_path():
    workflow_path = ROOT / ".github" / "workflows" / "npm-release.yml"
    if not workflow_path.is_file():
        pytest.skip("GitHub workflows are excluded from the runtime Docker image")
    workflow = workflow_path.read_text(encoding="utf-8")

    assert 'tags:\n      - "v*"' in workflow
    assert "scripts/release_artifacts.py verify-release" in workflow
    assert "git -c gpg.ssh.allowedSignersFile" not in workflow
    assert "git merge-base --is-ancestor" not in workflow
    assert "NPM_TRUSTED_PUBLISHING == 'enabled'" in workflow
    assert "environment:\n      name: npm" in workflow
    assert "id-token: write" in workflow
    assert "NODE_AUTH_TOKEN" not in workflow
    assert "npm@11.19.0" in workflow
    assert "scripts/npm_release.py verify-bundle" in workflow
    assert (
        "- name: Reuse immutable packages already published to npm\n"
        "        if: github.event_name != 'pull_request'\n"
        "        run: python scripts/npm_release.py reuse-published --pack-dir dist/npm-pack"
        in workflow
    )
    assert "scripts/npm_release.py publish-bundle" in workflow
    assert "scripts/npm_release.py smoke-registry" in workflow
    assert "npm audit signatures" not in workflow
    for target in (
        "darwin-arm64",
        "darwin-x64",
        "linux-arm64-gnu",
        "linux-x64-gnu",
        "win32-x64",
    ):
        assert target in workflow
    for runner in (
        "ubuntu-22.04",
        "ubuntu-22.04-arm",
        "macos-26",
        "macos-15-intel",
        "windows-2025",
    ):
        assert runner in workflow
    assert "persist-credentials: false" in workflow
    assert not re.search(r"uses:\s+[^\s#]+@v\d", workflow)
