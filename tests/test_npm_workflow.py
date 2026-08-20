from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_npm_workflow_is_validation_only_and_pinned():
    workflow = (ROOT / ".github" / "workflows" / "npm-package.yml").read_text(
        encoding="utf-8"
    )

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
