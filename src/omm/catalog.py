"""Signed recommendation catalog verification and local rollback snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from omm.atomic import atomic_write_text, locked
from omm.config import CATALOG_HISTORY_DIR, RECOMMEND_MODEL_PATH


class CatalogVerificationError(ValueError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_public_key(encoded_key: str) -> Ed25519PublicKey:
    try:
        raw_key = base64.b64decode(encoded_key, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw_key)
    except (ValueError, TypeError) as error:
        raise CatalogVerificationError("catalog public key is not valid base64 Ed25519") from error


def public_key_fingerprint(encoded_key: str) -> str:
    load_public_key(encoded_key)
    return sha256_bytes(base64.b64decode(encoded_key))[:16]


def verify_signed_artifact(
    content: bytes,
    manifest: object,
    encoded_public_key: str,
) -> dict:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise CatalogVerificationError("unsupported catalog manifest")
    expected_sha256 = manifest.get("artifact_sha256")
    if expected_sha256 != sha256_bytes(content):
        raise CatalogVerificationError("catalog artifact hash does not match manifest")
    signature_text = manifest.get("signature")
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError) as error:
        raise CatalogVerificationError("catalog signature is not valid base64") from error
    public_key = load_public_key(encoded_public_key)
    try:
        public_key.verify(signature, content)
    except InvalidSignature as error:
        raise CatalogVerificationError("catalog signature is invalid") from error
    return manifest


def archive_current_artifact(
    artifact_path: Path | None = None,
    history_dir: Path | None = None,
) -> Path | None:
    source = artifact_path or RECOMMEND_MODEL_PATH
    destination_dir = history_dir or CATALOG_HISTORY_DIR
    if not source.exists():
        return None
    try:
        content = source.read_bytes()
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{sha256_bytes(content)}.json"
        if not destination.exists():
            destination.write_bytes(content)
    except OSError:
        return None
    return destination


def snapshots(history_dir: Path | None = None) -> list[Path]:
    root = history_dir or CATALOG_HISTORY_DIR
    if not root.exists():
        return []
    return sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)


def rollback(
    *,
    artifact_path: Path | None = None,
    history_dir: Path | None = None,
) -> Path:
    destination = artifact_path or RECOMMEND_MODEL_PATH
    current_hash = sha256_bytes(destination.read_bytes()) if destination.exists() else None
    selected = next(
        (path for path in snapshots(history_dir) if path.stem != current_hash),
        None,
    )
    if selected is None:
        raise FileNotFoundError("no previous catalog snapshot is available")
    snapshot_text = selected.read_text(encoding="utf-8")
    payload = json.loads(snapshot_text)
    if not isinstance(payload, dict):
        raise CatalogVerificationError("catalog snapshot is not a JSON object")
    archive_current_artifact(destination, history_dir)
    # Same lock + retrying replace predictor.py uses to write this same
    # path: a raw write_bytes()+replace() here would skip the
    # PermissionError retry atomic_write_text already needed for Windows
    # AV/indexing transiently holding the destination open.
    with locked(destination):
        atomic_write_text(destination, snapshot_text)
    return selected
