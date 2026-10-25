"""This Mac's role in the cluster, kept in OMM_HOME/cluster/cluster.json."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import secrets
from typing import Literal

from omm import config

# The name is exo's network key: its libp2p pnet pre-shared key is SHA3 of it
# (exo 1.0.71 swarm.rs), and that port is reachable from the office network
# (firewall.py), so it carries 128 random bits. omm before 2026-10-25 made
# 32-bit names; those still load so status and leave work, but join refuses
# them and init replaces them.
NAMESPACE_RE = re.compile(r"^omm-[0-9a-f]{32}$")
LEGACY_NAMESPACE_RE = re.compile(r"^omm-[0-9a-f]{8}$")


@dataclass(frozen=True)
class ClusterSettings:
    role: Literal["head", "member"]
    namespace: str
    gateway_port: int
    known_nodes: tuple[str, ...] = ()


def settings_path() -> Path:
    return config.OMM_HOME / "cluster" / "cluster.json"


def new_namespace() -> str:
    return "omm-" + secrets.token_hex(16)


def is_legacy(namespace: str) -> bool:
    return bool(LEGACY_NAMESPACE_RE.match(namespace))


def load() -> ClusterSettings | None:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("role") not in {"head", "member"}:
        return None
    namespace, port = data.get("namespace"), data.get("gateway_port")
    if (not isinstance(namespace, str) or not (NAMESPACE_RE.match(namespace) or is_legacy(namespace))
            or not isinstance(port, int)):
        return None
    nodes = data.get("known_nodes") or []
    return ClusterSettings(data["role"], namespace, port,
                           tuple(n for n in nodes if isinstance(n, str)) if isinstance(nodes, list) else ())


def save(value: ClusterSettings) -> None:
    from omm.atomic import atomic_write_text

    data = asdict(value)
    data["known_nodes"] = list(value.known_nodes)
    atomic_write_text(settings_path(), json.dumps({"version": 1, **data}, ensure_ascii=False, indent=2) + "\n")


def clear() -> None:
    settings_path().unlink(missing_ok=True)
