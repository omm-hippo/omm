#!/usr/bin/env python3
"""Generate Ed25519 catalog keys and create signed artifact manifests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _write(path: Path, content: str, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content + "\n", encoding="utf-8")
    if private:
        path.chmod(0o600)


def generate_keys(private_path: Path, public_path: Path) -> None:
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
    _write(public_path, base64.b64encode(public_raw).decode())


def sign(artifact: Path, private_path: Path, manifest_path: Path) -> None:
    content = artifact.read_bytes()
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
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


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
