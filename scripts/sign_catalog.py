#!/usr/bin/env python3
"""Generate Ed25519 catalog keys and create signed artifact manifests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _write(path: Path, content: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if private else 0o644
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def generate_keys(private_path: Path, public_path: Path) -> None:
    if private_path.absolute() == public_path.absolute():
        raise ValueError("private and public key paths must be different")
    if private_path.exists() or private_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite private key: {private_path}")
    if public_path.exists() or public_path.is_symlink():
        raise FileExistsError(f"refusing to overwrite public key: {public_path}")
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    _write(private_path, base64.b64encode(private_raw).decode(), private=True)
    try:
        _write(public_path, base64.b64encode(public_raw).decode())
    except BaseException:
        private_path.unlink(missing_ok=True)
        raise


def sign(artifact: Path, private_path: Path, manifest_path: Path) -> None:
    content = artifact.read_bytes()
    try:
        payload = json.loads(content)
    except (UnicodeError, ValueError) as error:
        raise ValueError(f"catalog artifact is not valid JSON: {artifact}") from error
    if not isinstance(payload, dict):
        raise ValueError("catalog artifact must contain a JSON object")
    if not private_path.is_file() or private_path.is_symlink():
        raise ValueError("catalog private key must be a regular file")
    private_raw = base64.b64decode(private_path.read_text(encoding="utf-8").strip(), validate=True)
    private_key = Ed25519PrivateKey.from_private_bytes(private_raw)
    signature = private_key.sign(content)
    manifest = {
        "schema_version": 1,
        "artifact": artifact.name,
        "artifact_sha256": hashlib.sha256(content).hexdigest(),
        "signature": base64.b64encode(signature).decode(),
        "signed_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(manifest_path, json.dumps(manifest, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="Create a new raw Ed25519 key pair.")
    generate.add_argument("--private", type=Path, required=True)
    generate.add_argument("--public", type=Path, required=True)
    signing = subparsers.add_parser("sign", help="Sign one JSON artifact.")
    signing.add_argument("artifact", type=Path)
    signing.add_argument("--private", type=Path, required=True)
    signing.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "generate":
        generate_keys(args.private, args.public)
    else:
        sign(args.artifact, args.private, args.manifest)


if __name__ == "__main__":
    main()
