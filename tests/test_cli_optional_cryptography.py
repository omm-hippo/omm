from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _environment_with_broken_cryptography(
    tmp_path: Path,
    error_type: str = "ImportError",
) -> dict[str, str]:
    package = tmp_path / "cryptography"
    package.mkdir()
    (package / "__init__.py").write_text(
        f'raise {error_type}("simulated mis-aligned LINKEDIT string pool")\n',
        encoding="utf-8",
    )
    environment = os.environ.copy()
    python_path = [str(tmp_path), str(REPOSITORY_ROOT / "src")]
    if environment.get("PYTHONPATH"):
        python_path.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_path)
    return environment


def test_cli_help_survives_unloadable_cryptography_extension(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from omm.cli import app; app(prog_name='omm')",
            "--help",
        ],
        cwd=REPOSITORY_ROOT,
        env=_environment_with_broken_cryptography(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "Example usage:" in result.stdout


@pytest.mark.parametrize("error_type", ["ImportError", "OSError"])
def test_catalog_verification_fails_closed_when_cryptography_cannot_load(
    tmp_path,
    error_type,
):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from omm import catalog"
                "\ncontent = b'{}'"
                "\nmanifest = {'schema_version': 1, "
                "'artifact_sha256': catalog.sha256_bytes(content), 'signature': 'AA=='}"
                "\ntry: catalog.verify_signed_artifact(content, manifest, 'AA==')"
                "\nexcept catalog.CatalogVerificationError as error: print(error)"
                "\nelse: raise SystemExit('expected CatalogVerificationError')"
            ),
        ],
        cwd=REPOSITORY_ROOT,
        env=_environment_with_broken_cryptography(tmp_path, error_type),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "cryptography could not load" in result.stdout
    assert "pipx install omm-model" in result.stdout
