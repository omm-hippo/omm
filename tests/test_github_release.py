"""Exercise the real publisher shell against a local fake GitHub destination.

These tests verify orchestration and persisted asset bytes, not live GitHub or
WinGet installation. No command in the fake client can access the network.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest

from scripts import release_artifacts


ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.3"
PYTHON_ASSETS = (f"omm_model-{VERSION}-py3-none-any.whl", f"omm_model-{VERSION}.tar.gz")
WINDOWS_ASSET = f"omm-windows-x64-{VERSION}.zip"


FAKE_GH = r'''
import json
import os
from pathlib import Path
import re
import shutil
import sys

root = Path(os.environ["FAKE_GH_ROOT"])
args = sys.argv[1:]
with (root / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")

def option(name):
    return args[args.index(name) + 1]

def release():
    path = root / "release.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data["assets"] = [{"name": p.name} for p in sorted((root / "assets").iterdir())]
    return data

if args[:1] == ["api"]:
    data = release()
    if "--paginate" in args:
        print(json.dumps([[data] if data else []]))
    else:
        assert data and "releases/7" in args[1], args
        query = option("--jq")
        if query == ".assets | length":
            print(len(data["assets"]))
        else:
            name = re.search(r'select\(\.name == "([^"]+)"\)', query).group(1)
            print(sum(a["name"] == name for a in data["assets"]))
elif args[:2] == ["release", "create"]:
    assert release() is None and "--draft" in args and "--verify-tag" in args
    (root / "release.json").write_text(json.dumps({"id": 7, "tag_name": args[2], "draft": True}))
elif args[:2] == ["release", "upload"]:
    source = Path(args[3])
    target = root / "assets" / source.name
    assert release() and release()["draft"] and not target.exists(), args
    shutil.copyfile(source, target)
elif args[:2] == ["release", "download"]:
    name = option("--pattern")
    source = root / "assets" / name
    destination = Path(option("--dir")) / name
    shutil.copyfile(source, destination)
    if os.environ.get("FAKE_GH_CORRUPT_DOWNLOAD") == name:
        destination.write_bytes(b"corrupted in transit")
elif args[:2] == ["release", "edit"]:
    assert "--draft=false" in args and release(), args
    data = release()
    data["draft"] = False
    (root / "release.json").write_text(json.dumps(data))
else:
    raise AssertionError(f"unexpected fake GitHub command: {args!r}")
'''


def _bundle(path: Path, asset_set: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    names = PYTHON_ASSETS if asset_set == "python" else (WINDOWS_ASSET,)
    lines = []
    for name in names:
        content = f"validated fixture bytes for {name}".encode()
        (path / name).write_bytes(content)
        lines.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
    manifest = "SHA256SUMS" if asset_set == "python" else f"{WINDOWS_ASSET}.sha256"
    (path / manifest).write_text("".join(lines), encoding="utf-8")


@pytest.fixture
def publisher(tmp_path):
    workflow = ROOT / ".github/workflows/github-release.yml"
    if not workflow.is_file():
        pytest.skip("GitHub workflow files are excluded from the Docker build context")
    if os.name == "nt" or not shutil.which("bash") or not shutil.which("jq"):
        pytest.skip("publisher runs on Ubuntu and requires POSIX bash and jq")
    script = workflow.read_text().split(
        "      - name: Publish only after all five immutable assets are present\n", 1
    )[1].split("        run: |\n", 1)[1].split("\n  verify-winget-install:", 1)[0]
    script = textwrap.dedent(script)
    destination = tmp_path / "github"
    (destination / "assets").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    calls = []

    def run(asset_set, *, corrupt_download="", omit_local_checksum=False):
        working = tmp_path / f"call-{len(calls)}"
        calls.append(working)
        (working / "tooling/scripts").mkdir(parents=True)
        shutil.copyfile(
            ROOT / "scripts/release_artifacts.py", working / "tooling/scripts/release_artifacts.py"
        )
        _bundle(working / "release-assets", asset_set)
        if omit_local_checksum:
            checksum = working / "release-assets/SHA256SUMS"
            checksum.write_text(checksum.read_text().splitlines()[0] + "\n")
        output = working / "output"
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=working,
            env={
                **os.environ,
                "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                "FAKE_GH_ROOT": str(destination),
                "FAKE_GH_CORRUPT_DOWNLOAD": corrupt_download,
                "GH_REPO": "fixture/omm",
                "VERSION": VERSION,
                "ASSET_SET": asset_set,
                "GITHUB_OUTPUT": str(output),
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result, output.read_text() if output.exists() else ""

    return destination, run


@pytest.mark.parametrize("order", [("python", "windows"), ("windows", "python")])
def test_publisher_waits_for_both_sets_and_supports_identical_reruns(publisher, order):
    destination, publish = publisher
    first, second = order
    result, output = publish(first)
    assert result.returncode == 0, result.stderr
    assert output == "published=false\n"
    assert json.loads((destination / "release.json").read_text())["draft"] is True

    result, output = publish(second)
    assert result.returncode == 0, result.stderr
    assert output == "published=true\n"
    assert json.loads((destination / "release.json").read_text())["draft"] is False
    release_artifacts.validate_release_asset_checksums(destination / "assets", VERSION)
    before = {p.name: p.read_bytes() for p in (destination / "assets").iterdir()}

    result, output = publish(first)
    assert result.returncode == 0, result.stderr
    assert output == "published=true\n"
    assert before == {p.name: p.read_bytes() for p in (destination / "assets").iterdir()}


def test_publisher_rejects_incomplete_local_manifest_before_creating_release(publisher):
    destination, publish = publisher
    result, output = publish("python", omit_local_checksum=True)
    assert result.returncode != 0
    assert "checksum file covers" in result.stderr
    assert not (destination / "release.json").exists()
    assert not (destination / "calls.jsonl").exists()
    assert output == ""


def test_publisher_rejects_missing_checksum_for_the_other_workflows_archive(publisher):
    destination, publish = publisher
    result, _ = publish("python")
    assert result.returncode == 0, result.stderr
    manifest = destination / "assets/SHA256SUMS"
    manifest.write_text(manifest.read_text().splitlines()[0] + "\n")
    result, output = publish("windows")
    assert result.returncode != 0
    assert "checksum file covers" in result.stderr
    assert json.loads((destination / "release.json").read_text())["draft"] is True
    assert output == ""


def test_publisher_never_overwrites_conflicting_assets(publisher):
    destination, publish = publisher
    result, _ = publish("windows")
    assert result.returncode == 0, result.stderr
    archive = destination / "assets" / WINDOWS_ASSET
    archive.write_bytes(b"conflicting existing release bytes")
    result, output = publish("windows")
    assert result.returncode != 0
    assert archive.read_bytes() == b"conflicting existing release bytes"
    assert output == ""


def test_publisher_rejects_corrupted_download(publisher):
    destination, publish = publisher
    result, output = publish("windows", corrupt_download=WINDOWS_ASSET)
    assert result.returncode != 0
    assert json.loads((destination / "release.json").read_text())["draft"] is True
    assert output == ""


def test_publisher_resumes_a_partially_uploaded_draft(publisher):
    destination, publish = publisher
    (destination / "release.json").write_text(
        json.dumps({"id": 7, "tag_name": f"v{VERSION}", "draft": True})
    )
    _bundle(destination / "assets", "python")
    (destination / "assets/SHA256SUMS").unlink()
    result, output = publish("python")
    assert result.returncode == 0, result.stderr
    assert output == "published=false\n"
    release_artifacts.validate_release_asset_checksums(destination / "assets", VERSION, "python")


def test_publisher_refuses_to_extend_an_incomplete_public_release(publisher):
    destination, publish = publisher
    (destination / "release.json").write_text(
        json.dumps({"id": 7, "tag_name": f"v{VERSION}", "draft": False})
    )
    result, output = publish("windows")
    assert result.returncode != 0
    assert "refusing to modify" in result.stderr
    assert list((destination / "assets").iterdir()) == []
    assert output == ""


def test_winget_verification_belongs_to_whichever_workflow_completes_the_release():
    workflows = ROOT / ".github/workflows"
    if not workflows.is_dir():
        pytest.skip("GitHub workflow files are excluded from the Docker build context")
    shared = (workflows / "github-release.yml").read_text()
    job = shared.split("\n  verify-winget-install:\n", 1)[1]
    assert "needs: release" in job
    assert "needs.release.outputs.published == 'true'" in job
    assert "asset_set" not in job
    assert "download-artifact@" not in job  # no dependency on the caller's artifact store
    assert "gh release download" in job
    assert "gh attestation verify" in job
    assert "winget install --manifest" in job
    assert "winget uninstall --manifest" in job
    for caller in ("release.yml", "windows-portable.yml"):
        content = (workflows / caller).read_text()
        assert "uses: ./.github/workflows/github-release.yml" in content
        assert "\n  verify-winget-install:" not in content


@pytest.mark.parametrize("asset_set", ["python", "windows"])
def test_release_asset_checksums_detect_corruption(tmp_path, asset_set):
    _bundle(tmp_path, asset_set)
    name = PYTHON_ASSETS[0] if asset_set == "python" else WINDOWS_ASSET
    (tmp_path / name).write_bytes(b"corrupted")
    with pytest.raises(release_artifacts.ReleaseValidationError, match="checksum mismatch"):
        release_artifacts.validate_release_asset_checksums(tmp_path, VERSION, asset_set)


def test_release_asset_checksums_reject_duplicate_entries(tmp_path):
    _bundle(tmp_path, "windows")
    manifest = tmp_path / f"{WINDOWS_ASSET}.sha256"
    manifest.write_text(manifest.read_text() * 2)
    with pytest.raises(release_artifacts.ReleaseValidationError, match="duplicate checksum"):
        release_artifacts.validate_release_asset_checksums(tmp_path, VERSION, "windows")


def test_release_asset_checksums_reject_another_versions_assets(tmp_path):
    _bundle(tmp_path, "python")
    with pytest.raises(release_artifacts.ReleaseValidationError, match="must be a regular file"):
        release_artifacts.validate_release_asset_checksums(tmp_path, "1.2.4", "python")
