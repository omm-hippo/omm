"""Proofs between cluster Macs on the gateway's `/omm/member/*` routes.

Every Mac in the cluster holds the namespace (exo's pnet key), so both sides
derive one key from it and never send the namespace itself. A member signs
each request, and the head each reply, over the nonce, the TLS leaf
certificate the member saw and the body. With the leaf in the request proof,
a relay that ends the member's TLS with its own certificate cannot replay
the request to the head. The leaf stands in for a pinned CA, which a member
does not have: the CA changes on handover, and
a member may reach the head over the office network.
"""

from __future__ import annotations

import hashlib
import hmac
import threading

_KEY_LABEL = b"omm-cluster-member-link-v1"
WINDOW_SECONDS = 120

HEADER_NODE = "X-Omm-Node"
HEADER_TIME = "X-Omm-Time"
HEADER_NONCE = "X-Omm-Nonce"
HEADER_PROOF = "X-Omm-Proof"
HEADER_HEAD_PROOF = "X-Omm-Head-Proof"


def member_key(namespace: str) -> bytes:
    return hmac.new(namespace.encode("utf-8"), _KEY_LABEL, hashlib.sha256).digest()


def _mac(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _request_message(method, path, node_id, stamp, nonce, leaf_sha256, body_sha256) -> str:
    return f"member|{method}|{path}|{node_id}|{stamp}|{nonce}|{leaf_sha256}|{body_sha256}"


def sign_request(key: bytes, method: str, path: str, node_id: str, *, now: float, nonce: str, leaf_sha256: str,
                 body_sha256: str) -> dict[str, str]:
    stamp = str(int(now))
    return {
        HEADER_NODE: node_id,
        HEADER_TIME: stamp,
        HEADER_NONCE: nonce,
        HEADER_PROOF: _mac(key, _request_message(method, path, node_id, stamp, nonce, leaf_sha256, body_sha256)),
    }


class NonceCache:
    """Nonces seen inside the clock window, so a captured request cannot be replayed."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seen(self, nonce: str, now: float) -> bool:
        with self._lock:
            for old, at in list(self._seen.items()):
                if now - at > 2 * WINDOW_SECONDS:
                    del self._seen[old]
            if nonce in self._seen:
                return True
            self._seen[nonce] = now
            return False


def verify_request(key: bytes, method: str, path: str, headers, *, now: float, nonces: NonceCache, leaf_sha256: str,
                   body_sha256: str) -> str | None:
    """The calling member's node id, or None when the request is not proven for this leaf and body."""
    node, stamp, nonce, proof = (headers.get(name) for name in (HEADER_NODE, HEADER_TIME, HEADER_NONCE, HEADER_PROOF))
    if not (node and stamp and nonce and proof):
        return None
    try:
        sent = int(stamp)
    except ValueError:
        return None
    if abs(now - sent) > WINDOW_SECONDS:
        return None
    expected = _mac(key, _request_message(method, path, node, stamp, nonce, leaf_sha256, body_sha256))
    if not hmac.compare_digest(proof, expected):
        return None
    if nonces.seen(nonce, now):
        return None
    return node


def head_proof(key: bytes, nonce: str, leaf_sha256: str, body_sha256: str) -> str:
    return _mac(key, f"head|{nonce}|{leaf_sha256}|{body_sha256}")


def verify_head(key: bytes, nonce: str, leaf_sha256: str, body_sha256: str, proof: str | None) -> bool:
    return bool(proof) and hmac.compare_digest(proof, head_proof(key, nonce, leaf_sha256, body_sha256))


def leaf_sha256(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()
