#!/usr/bin/env python3
"""Aggregate collected arena votes into the signed leaderboard artifact.

Sub-project C's entry point (issue #409): fetch or load vote rows, score them
with scripts/arena_score.py, run scripts/arena_quality_gate.py, and write
published/arena-leaderboard.json. Signing is a separate workflow step that
calls scripts/sign_catalog.py, exactly as train.yml does for the
recommendation model.

Spec: docs/superpowers/specs/2026-09-26-arena-vote-aggregation-design.md
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from omm.atomic import atomic_write_text  # noqa: E402
from scripts import arena_quality_gate  # noqa: E402
from scripts.arena_score import BOOTSTRAP_RESAMPLES, build_artifact  # noqa: E402
from scripts.retrain_decision import append_github_outputs  # noqa: E402

#: Fetch cap, as train_model.py has MAX_REAL_ROWS.
ARENA_MAX_ROWS = 100000

_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

#: Where the service-account assertion is exchanged. Fixed rather than read
#: from the key file's `token_uri`, so a tampered key file cannot send the
#: signed assertion to a host of its choosing. Same endpoint and scopes as
#: cf-worker/src/rtdb.ts, which mints the Worker's write token the same way.
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_RTDB_SCOPES = (
    "https://www.googleapis.com/auth/firebase.database "
    "https://www.googleapis.com/auth/userinfo.email"
)


class VotesFetchError(RuntimeError):
    """The vote export could not be fetched or parsed (not an empty corpus)."""


def is_firebase_rtdb_json_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    official = hostname.endswith(".firebaseio.com") or hostname.endswith(
        ".firebasedatabase.app"
    )
    return parsed.scheme == "https" and official and parsed.path.endswith(".json")


def extract_rows(payload: object) -> list[dict]:
    """Accept every shape an RTDB export arrives in.

    A dict keyed by PoW digest, a {"votes": {...}} wrapper, or a bare list.
    Parsing only one of them would turn a shape change into a silent empty
    corpus that the gate reads as "no data yet".
    """
    if isinstance(payload, dict):
        inner = payload.get("votes")
        if isinstance(inner, (dict, list)):
            return extract_rows(inner)
        return [row for row in payload.values() if isinstance(row, dict)]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def access_token_from_service_account(raw: str, *, now: int | None = None) -> str:
    """Mint a short-lived OAuth2 access token from a service-account key.

    A Google access token lives one hour, so storing one as a CI secret means
    it has expired before the first nightly run. The workflow stores the
    service-account key JSON instead and this mints a fresh token per run.

    Error messages never quote the key file or the token endpoint's response
    body beyond its status, so the private key cannot end up in CI output.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    try:
        account = json.loads(raw)
    except ValueError:
        raise VotesFetchError("the service account key is not valid JSON") from None
    if not isinstance(account, dict):
        raise VotesFetchError("the service account key is not a JSON object")
    client_email = account.get("client_email")
    private_key = account.get("private_key")
    if not isinstance(client_email, str) or not client_email:
        raise VotesFetchError("the service account key has no client_email")
    if not isinstance(private_key, str) or not private_key:
        raise VotesFetchError("the service account key has no private_key")
    try:
        key = serialization.load_pem_private_key(private_key.encode(), password=None)
    except (ValueError, TypeError):
        raise VotesFetchError("the service account private_key is not a PEM key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise VotesFetchError("the service account private_key is not an RSA key")

    issued_at = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(
        json.dumps(
            {
                "iss": client_email,
                "scope": _RTDB_SCOPES,
                "aud": _GOOGLE_TOKEN_URL,
                "iat": issued_at,
                "exp": issued_at + 3600,
            }
        ).encode()
    )
    signing_input = f"{header}.{claims}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    assertion = f"{header}.{claims}.{_b64url(signature)}"

    try:
        response = requests.post(
            _GOOGLE_TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
            timeout=30,
            # Never follow a redirect off oauth2.googleapis.com with the
            # signed assertion in the body.
            allow_redirects=False,
        )
    except requests.RequestException as error:
        raise VotesFetchError(f"token exchange failed: {type(error).__name__}") from None
    if response.status_code != 200:
        raise VotesFetchError(f"token exchange failed: HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise VotesFetchError("token exchange returned non-JSON") from None
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise VotesFetchError("token exchange returned no access_token")
    return token


def fetch_votes(
    url: str, token: str | None, *, limit: int = ARENA_MAX_ROWS
) -> list[dict]:
    """Read the private `votes` node.

    This deliberately does NOT reuse train_model.fetch_real_rows. That function
    refuses to send a credential to a Firebase host because the `telemetry`
    node is world-readable, so attaching one would leak it for nothing. The
    `votes` node is `.read: false`, so an authenticated read is mandatory - the
    opposite requirement, and a missing credential is a hard error rather than
    an empty corpus.

    The credential goes in the Authorization header, never in a query
    parameter, so it cannot end up in a request log or in CI output.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and hostname in _LOOPBACK
    ):
        raise VotesFetchError("votes URL must use HTTPS (HTTP only for loopback)")
    if not token and hostname not in _LOOPBACK:
        raise VotesFetchError(
            "a credential is required to read the private votes node "
            "(set LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON)"
        )
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params: dict[str, object] = {}
    if is_firebase_rtdb_json_url(url):
        # RTDB keys here are PoW digests, not timestamps, so this is a uniform
        # sample of the corpus. Do not describe it as "the newest rows".
        params = {"orderBy": '"$key"', "limitToLast": limit}
    try:
        response = requests.get(url, headers=headers, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise VotesFetchError(
            f"couldn't fetch votes from {parsed.scheme}://{hostname}{parsed.path}: "
            f"{error}"
        ) from error
    return extract_rows(payload)[-limit:]


def load_votes_file(path: Path) -> list[dict]:
    """Read JSONL or Firebase-shaped JSON from disk.

    This is how every test and every pre-deployment run gets its corpus.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        return extract_rows(json.loads(raw))
    except ValueError:
        pass
    rows = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError as error:
            raise VotesFetchError(f"{path}: not JSON or JSONL: {error}") from error
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _load_json(path: Path) -> dict | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _serialize(artifact: dict) -> str:
    return json.dumps(artifact, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--votes-url")
    source.add_argument("--votes-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--incumbent", type=Path)
    parser.add_argument("--quality-gate", action="store_true")
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--resamples", type=int, default=BOOTSTRAP_RESAMPLES)
    parser.add_argument(
        "--generated-at",
        help="Override the timestamp, so a rerun on an unchanged corpus is "
        "byte-identical. The workflow leaves this unset.",
    )
    args = parser.parse_args(argv)

    if args.votes_file:
        rows = load_votes_file(args.votes_file)
    else:
        service_account = os.environ.get("LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON")
        token = (
            access_token_from_service_account(service_account)
            if service_account
            else None
        )
        rows = fetch_votes(args.votes_url, token)

    generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
    artifact = build_artifact(rows, generated_at=generated_at, resamples=args.resamples)

    incumbent = _load_json(args.incumbent) if args.incumbent else None
    result = arena_quality_gate.evaluate(artifact, incumbent)
    outputs = {"gate_status": result.status}

    if args.quality_gate and result.status == "blocked":
        print(f"Arena leaderboard not published ({len(result.reasons)} reason(s)):")
        for reason in result.reasons:
            print(f"  - {reason}")
        outputs["artifact_changed"] = "false"
        if args.github_output:
            append_github_outputs(args.github_output, outputs)
        return 0

    serialized = _serialize(artifact)
    previous = None
    if args.output.exists():
        try:
            previous = args.output.read_text(encoding="utf-8")
        except OSError:
            previous = None
    changed = serialized != previous
    if changed:
        atomic_write_text(args.output, serialized)
    outputs["artifact_changed"] = "true" if changed else "false"
    print(
        f"Arena leaderboard: {len(artifact['models'])} models, "
        f"{artifact['corpus']['effective_votes']:.1f} effective votes, "
        f"gate {result.status}, changed {outputs['artifact_changed']}"
    )
    if args.github_output:
        append_github_outputs(args.github_output, outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
