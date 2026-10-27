"""HTTPS helpers for tests that run a real cluster GatewayServer (#536)."""

from __future__ import annotations

import http.client

from omm import rag_remote
from omm.cluster import tls


def url(server) -> str:
    return f"https://127.0.0.1:{server.server_address[1]}"


def pin(server) -> str:
    """Leave behind what `omm rag remote set` pins after the user accepts the fingerprint."""
    rag_remote.save_ca(server.certificates.ca_pem().decode())
    return str(rag_remote.ca_path())


def connection(server, timeout: float = 10) -> http.client.HTTPSConnection:
    context = tls.client_context(cadata=server.certificates.ca_pem().decode())
    return http.client.HTTPSConnection("127.0.0.1", server.server_address[1], timeout=timeout, context=context)
