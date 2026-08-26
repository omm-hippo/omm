"""Signed recommendation catalog verification and local rollback snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from omm.atomic import atomic_write_bytes, atomic_write_text, locked
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
    *,
    require_signed: bool = False,
) -> Path | None:
    source = artifact_path or RECOMMEND_MODEL_PATH
    destination_dir = history_dir or CATALOG_HISTORY_DIR
    if not source.exists():
        return None
    try:
        content = source.read_bytes()
        provenance_path = source.with_suffix(source.suffix + ".provenance.json")
        provenance: dict | None = None
        if provenance_path.exists():
            loaded = json.loads(provenance_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                provenance = loaded
        if require_signed:
            if provenance is None:
                return None
            verify_signed_artifact(
                content, provenance.get("manifest"), provenance.get("public_key")
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        digest = sha256_bytes(content)
        destination = destination_dir / f"{digest}.json"
        if not destination.exists():
            atomic_write_bytes(destination, content)
        if provenance is not None:
            provenance_destination = destination_dir / f"{digest}.provenance.json"
            if not provenance_destination.exists():
                atomic_write_text(
                    provenance_destination,
                    json.dumps(provenance, sort_keys=True) + "\n",
                )
    except (OSError, ValueError, TypeError):
        return None
    return destination


def snapshots(history_dir: Path | None = None) -> list[Path]:
    root = history_dir or CATALOG_HISTORY_DIR
    if not root.exists():
        return []
    candidates = [
        path for path in root.glob("*.json")
        if re.fullmatch(r"[0-9a-f]{64}\.json", path.name)
    ]
    def modified_at(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return float("-inf")

    return sorted(candidates, key=modified_at, reverse=True)


def rollback(
    *,
    artifact_path: Path | None = None,
    history_dir: Path | None = None,
    require_signed: bool = False,
) -> Path:
    destination = artifact_path or RECOMMEND_MODEL_PATH
    current_hash = sha256_bytes(destination.read_bytes()) if destination.exists() else None
    selected = None
    snapshot_content = None
    for path in snapshots(history_dir):
        if path.stem == current_hash:
            continue
        provenance_path = path.with_name(f"{path.stem}.provenance.json")
        try:
            candidate_content = path.read_bytes()
            payload = json.loads(candidate_content)
            if not isinstance(payload, dict):
                continue
            if require_signed:
                provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
                verify_signed_artifact(
                    candidate_content,
                    provenance.get("manifest"),
                    provenance.get("public_key"),
                )
        except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
            # A partial/corrupt newest snapshot must not hide an older valid
            # rollback point.
            continue
        selected = path
        snapshot_content = candidate_content
        break
    if selected is None or snapshot_content is None:
        raise FileNotFoundError("no previous catalog snapshot is available")
    archive_current_artifact(destination, history_dir, require_signed=require_signed)
    # Same lock + retrying replace predictor.py uses to write this same
    # path: a raw write_bytes()+replace() here would skip the
    # PermissionError retry the atomic writer already provides for Windows
    # AV/indexing transiently holding the destination open.
    with locked(destination):
        atomic_write_bytes(destination, snapshot_content)
        destination_provenance = destination.with_suffix(
            destination.suffix + ".provenance.json"
        )
        selected_provenance = selected.with_name(f"{selected.stem}.provenance.json")
        if selected_provenance.exists():
            atomic_write_text(
                destination_provenance,
                selected_provenance.read_text(encoding="utf-8"),
            )
        else:
            destination_provenance.unlink(missing_ok=True)
    return selected
