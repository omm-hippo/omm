"""The arena aggregation CLI: fetching, gating, and writing the artifact."""

from __future__ import annotations

import json

import pytest

from scripts import aggregate_arena_votes as aggregate


def _row(index: int, client: str, winner: str) -> dict:
    return {
        "schema_version": 1,
        "battle_id": f"{index:08d}-1111-1111-1111-111111111111",
        "client_id": client,
        "recorded_at": "2026-09-26T10:00:00+00:00",
        "engine": "ollama",
        "winner": winner,
        "model_filename_a": f"alpha-{index % 6}.gguf",
        "model_filename_b": f"beta-{index % 5}.gguf",
        "elapsed_a": 4.0,
        "elapsed_b": 5.0,
        "tokens_a": 100,
        "tokens_b": 120,
        "tokens_per_second_a": 40.0,
        "tokens_per_second_b": 20.0,
        "memory_gb_a": 4.0,
        "memory_gb_b": 4.0,
    }


def _healthy_rows(count: int = 600) -> list[dict]:
    return [
        _row(index, f"{index:08x}", "a" if index % 3 else "b") for index in range(count)
    ]


def test_a_firebase_dict_export_is_parsed():
    rows = aggregate.extract_rows({"powdigest1": _row(0, "aa", "a")})
    assert len(rows) == 1
    assert rows[0]["battle_id"].startswith("00000000")


def test_a_votes_wrapped_export_is_parsed():
    rows = aggregate.extract_rows({"votes": {"powdigest1": _row(0, "aa", "a")}})
    assert len(rows) == 1


def test_a_bare_list_export_is_parsed():
    assert len(aggregate.extract_rows([_row(0, "aa", "a")])) == 1


def test_a_null_export_is_an_empty_corpus():
    assert aggregate.extract_rows(None) == []


def test_non_dict_entries_are_ignored():
    assert aggregate.extract_rows({"a": _row(0, "aa", "a"), "b": "junk"}) == [
        _row(0, "aa", "a")
    ]


def test_an_https_firebase_json_url_is_accepted():
    assert aggregate.is_firebase_rtdb_json_url(
        "https://localfit-8ab57.firebaseio.com/votes.json"
    )


def test_a_non_firebase_host_is_rejected():
    assert not aggregate.is_firebase_rtdb_json_url("https://example.com/votes.json")


def test_a_path_without_json_is_rejected():
    assert not aggregate.is_firebase_rtdb_json_url(
        "https://localfit-8ab57.firebaseio.com/votes"
    )


def test_a_missing_credential_for_a_private_node_is_a_hard_error():
    # An unauthenticated read of a .read:false node returns 401. Treating that
    # as "no votes yet" would publish an empty leaderboard.
    with pytest.raises(aggregate.VotesFetchError, match="credential"):
        aggregate.fetch_votes("https://localfit-8ab57.firebaseio.com/votes.json", None)


def test_the_credential_travels_in_the_header_not_the_url(monkeypatch):
    seen = {}

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"digest": _row(0, "aa", "a")}

    def _get(url, headers=None, params=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers or {}
        seen["params"] = params or {}
        return _Response()

    monkeypatch.setattr(aggregate.requests, "get", _get)
    aggregate.fetch_votes("https://localfit-8ab57.firebaseio.com/votes.json", "tok")
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert "tok" not in seen["url"]
    assert "tok" not in json.dumps(seen["params"])
    assert seen["params"]["orderBy"] == '"$key"'


# --- service-account credential ------------------------------------------
#
# A Google OAuth2 access token lives one hour, so a token stored as a secret is
# dead before the first nightly run. The workflow stores the service-account
# key instead and mints a fresh token every run.


def _service_account() -> tuple[str, object]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    raw = json.dumps(
        {
            "type": "service_account",
            "client_email": "votes-reader@localfit-8ab57.iam.gserviceaccount.com",
            "private_key": pem,
            # A tampered key file must not be able to redirect the signed
            # assertion to a host of its choosing.
            "token_uri": "https://evil.example/token",
        }
    )
    return raw, key.public_key()


def _b64url_decode(part: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class _TokenResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_a_service_account_mints_a_signed_jwt_bearer_exchange(monkeypatch):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    raw, public_key = _service_account()
    seen = {}

    def _post(url, data=None, timeout=None, allow_redirects=True):
        seen.update(url=url, data=data, allow_redirects=allow_redirects)
        return _TokenResponse(200, {"access_token": "ya29.fresh", "expires_in": 3599})

    monkeypatch.setattr(aggregate.requests, "post", _post)
    token = aggregate.access_token_from_service_account(raw, now=1_000_000)

    assert token == "ya29.fresh"
    assert seen["url"] == "https://oauth2.googleapis.com/token"
    assert seen["allow_redirects"] is False
    assert seen["data"]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header, claims, signature = seen["data"]["assertion"].split(".")
    assert json.loads(_b64url_decode(header)) == {"alg": "RS256", "typ": "JWT"}
    decoded = json.loads(_b64url_decode(claims))
    assert decoded["iss"] == "votes-reader@localfit-8ab57.iam.gserviceaccount.com"
    assert decoded["aud"] == "https://oauth2.googleapis.com/token"
    assert "https://www.googleapis.com/auth/firebase.database" in decoded["scope"].split()
    assert decoded["iat"] == 1_000_000
    assert decoded["exp"] == 1_000_000 + 3600
    public_key.verify(
        _b64url_decode(signature),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["a list"]),
        json.dumps({"client_email": "x@y"}),
        json.dumps({"client_email": "x@y", "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"}),
    ],
)
def test_a_malformed_service_account_is_a_hard_error(raw, monkeypatch):
    monkeypatch.setattr(
        aggregate.requests, "post", lambda *a, **k: pytest.fail("must not reach the network")
    )
    with pytest.raises(aggregate.VotesFetchError, match="service account"):
        aggregate.access_token_from_service_account(raw)


@pytest.mark.parametrize(
    "response",
    [
        _TokenResponse(400, {"error": "invalid_grant"}),
        _TokenResponse(302, {}),
        _TokenResponse(200, {"expires_in": 3599}),
        _TokenResponse(200, {"access_token": "", "expires_in": 3599}),
    ],
)
def test_a_failed_token_exchange_is_a_hard_error(response, monkeypatch):
    raw, _ = _service_account()
    monkeypatch.setattr(aggregate.requests, "post", lambda *a, **k: response)
    with pytest.raises(aggregate.VotesFetchError, match="token") as error:
        aggregate.access_token_from_service_account(raw)
    assert "PRIVATE KEY" not in str(error.value)


def test_a_network_error_during_the_exchange_is_a_hard_error(monkeypatch):
    raw, _ = _service_account()

    def _post(*args, **kwargs):
        raise aggregate.requests.ConnectionError("boom")

    monkeypatch.setattr(aggregate.requests, "post", _post)
    with pytest.raises(aggregate.VotesFetchError, match="token"):
        aggregate.access_token_from_service_account(raw)


def test_main_reads_with_a_token_minted_from_the_service_account(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON", "{sa}")
    minted = []
    monkeypatch.setattr(
        aggregate,
        "access_token_from_service_account",
        lambda raw: minted.append(raw) or "ya29.fresh",
    )
    seen = {}

    def _fetch(url, token):
        seen.update(url=url, token=token)
        return _healthy_rows()

    monkeypatch.setattr(aggregate, "fetch_votes", _fetch)
    exit_code = aggregate.main(
        [
            "--votes-url",
            "https://localfit-8ab57.firebaseio.com/votes.json",
            "--output",
            str(tmp_path / "arena-leaderboard.json"),
            "--resamples",
            "20",
            "--generated-at",
            "2026-09-26T00:00:00+00:00",
        ]
    )
    assert exit_code == 0
    assert minted == ["{sa}"]
    assert seen["token"] == "ya29.fresh"


def test_main_without_a_service_account_fails_before_reading(monkeypatch):
    monkeypatch.delenv("LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.setattr(
        aggregate.requests, "get", lambda *a, **k: pytest.fail("must not read unauthenticated")
    )
    with pytest.raises(aggregate.VotesFetchError, match="LOCALFIT_VOTES_SERVICE_ACCOUNT_JSON"):
        aggregate.main(
            [
                "--votes-url",
                "https://localfit-8ab57.firebaseio.com/votes.json",
                "--output",
                "unused.json",
            ]
        )


def test_a_jsonl_file_is_loaded(tmp_path):
    path = tmp_path / "votes.jsonl"
    path.write_text(
        "\n".join(json.dumps(_row(index, "aa", "a")) for index in range(3)),
        encoding="utf-8",
    )
    assert len(aggregate.load_votes_file(path)) == 3


def test_a_firebase_shaped_json_file_is_loaded(tmp_path):
    path = tmp_path / "votes.json"
    path.write_text(json.dumps({"digest": _row(0, "aa", "a")}), encoding="utf-8")
    assert len(aggregate.load_votes_file(path)) == 1


def test_a_passing_run_writes_the_artifact(tmp_path):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    outputs = tmp_path / "github-output"
    exit_code = aggregate.main(
        [
            "--votes-file",
            str(votes),
            "--output",
            str(output),
            "--quality-gate",
            "--github-output",
            str(outputs),
            "--resamples",
            "10",
        ]
    )
    assert exit_code == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 2
    assert artifact["models"]
    assert artifact["status"] == "ranked" and artifact["ranked"] is True
    assert "status=ranked" in outputs.read_text(encoding="utf-8")
    assert "artifact_changed=true" in outputs.read_text(encoding="utf-8")


def _run(tmp_path, rows, *extra, name="votes.jsonl"):
    votes = tmp_path / name
    votes.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    output = tmp_path / "arena-leaderboard.json"
    outputs = tmp_path / "github-output"
    outputs.unlink(missing_ok=True)
    exit_code = aggregate.main(
        [
            "--votes-file", str(votes),
            "--output", str(output),
            "--quality-gate",
            "--github-output", str(outputs),
            "--resamples", "5",
            *extra,
        ]
    )
    return exit_code, output, outputs.read_text(encoding="utf-8")


def test_a_first_blocked_run_writes_a_collecting_file(tmp_path, capsys):
    exit_code, output, outputs = _run(tmp_path, [_row(0, "aa", "a"), _row(1, "bb", "b")])
    assert exit_code == 0
    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["status"] == "collecting"
    assert artifact["models"] == []
    assert artifact["corpus"]["rows_used"] == 2
    assert artifact["corpus"]["client_count"] == 2
    assert any("effective votes" in reason for reason in artifact["reasons"])
    assert "status=collecting" in outputs
    assert "artifact_changed=true" in outputs
    assert "effective votes" in capsys.readouterr().out


def test_a_quiet_night_writes_nothing(tmp_path):
    rows = [_row(0, "aa", "a")]
    _run(tmp_path, rows, "--generated-at", "2026-10-16T04:00:00+00:00")
    output = tmp_path / "arena-leaderboard.json"
    first = output.read_bytes()
    _code, _output, outputs = _run(tmp_path, rows, "--generated-at", "2026-10-17T04:00:00+00:00")
    assert output.read_bytes() == first
    assert "artifact_changed=false" in outputs


def test_a_thin_night_keeps_the_ranked_board_without_an_incumbent_flag(tmp_path):
    """The previous output doubles as the incumbent, so a run that forgot
    --incumbent still cannot blank the public board."""
    _run(tmp_path, _healthy_rows(), "--generated-at", "2026-10-16T04:00:00+00:00")
    output = tmp_path / "arena-leaderboard.json"
    ranked = json.loads(output.read_text(encoding="utf-8"))
    _code, _output, outputs = _run(
        tmp_path, [_row(0, "aa", "a")], "--generated-at", "2026-10-17T04:00:00+00:00",
        name="thin.jsonl",
    )
    kept = json.loads(output.read_text(encoding="utf-8"))
    assert kept["status"] == "ranked"
    assert kept["models"] == ranked["models"]
    assert "artifact_changed=false" in outputs


def test_a_collecting_result_never_replaces_a_schema_1_file(tmp_path, capsys):
    output = tmp_path / "arena-leaderboard.json"
    legacy = json.dumps({"schema_version": 1, "models": [{"key": "x"}]})
    output.write_text(legacy, encoding="utf-8")
    _code, _output, outputs = _run(tmp_path, [_row(0, "aa", "a")])
    assert output.read_text(encoding="utf-8") == legacy
    assert "artifact_changed=false" in outputs
    assert "schema" in capsys.readouterr().out


def test_a_ranked_result_replaces_a_schema_1_file(tmp_path):
    output = tmp_path / "arena-leaderboard.json"
    output.write_text(json.dumps({"schema_version": 1, "models": []}), encoding="utf-8")
    _code, _output, outputs = _run(tmp_path, _healthy_rows())
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 2
    assert "artifact_changed=true" in outputs


def test_an_unchanged_corpus_reports_no_change(tmp_path):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    args = [
        "--votes-file",
        str(votes),
        "--output",
        str(output),
        "--quality-gate",
        "--resamples",
        "10",
        "--generated-at",
        "2026-09-27T04:00:00+00:00",
    ]
    assert aggregate.main(args) == 0
    first = output.read_text(encoding="utf-8")
    outputs = tmp_path / "github-output"
    assert aggregate.main(args + ["--github-output", str(outputs)]) == 0
    assert output.read_text(encoding="utf-8") == first
    assert "artifact_changed=false" in outputs.read_text(encoding="utf-8")


def test_the_written_artifact_verifies_against_its_signature(tmp_path):
    import subprocess
    import sys

    from omm import catalog

    votes = tmp_path / "votes.jsonl"
    votes.write_text(
        "\n".join(json.dumps(row) for row in _healthy_rows()), encoding="utf-8"
    )
    output = tmp_path / "arena-leaderboard.json"
    assert (
        aggregate.main(
            ["--votes-file", str(votes), "--output", str(output), "--resamples", "10"]
        )
        == 0
    )

    private = tmp_path / "key"
    public = tmp_path / "key.pub"
    subprocess.run(
        [
            sys.executable,
            "scripts/sign_catalog.py",
            "generate",
            "--private",
            str(private),
            "--public",
            str(public),
        ],
        check=True,
    )
    manifest = tmp_path / "arena-leaderboard.manifest.json"
    subprocess.run(
        [
            sys.executable,
            "scripts/sign_catalog.py",
            "sign",
            str(output),
            "--private",
            str(private),
            "--manifest",
            str(manifest),
            "--public",
            str(public),
        ],
        check=True,
    )
    catalog.verify_signed_artifact(
        output.read_bytes(),
        json.loads(manifest.read_text(encoding="utf-8")),
        public.read_text(encoding="utf-8").strip(),
    )


def test_the_written_collecting_file_verifies_against_its_signature(tmp_path):
    """The workflow signs a collecting file exactly like a ranked board."""
    import subprocess
    import sys

    from omm import catalog

    _code, output, _outputs = _run(tmp_path, [_row(0, "aa", "a"), _row(1, "bb", "b")])
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "collecting"
    private, public = tmp_path / "key", tmp_path / "key.pub"
    manifest = tmp_path / "arena-leaderboard.manifest.json"
    script = "scripts/sign_catalog.py"
    subprocess.run(
        [sys.executable, script, "generate", "--private", str(private), "--public", str(public)],
        check=True,
    )
    subprocess.run(
        [sys.executable, script, "sign", str(output), "--private", str(private),
         "--manifest", str(manifest), "--public", str(public)],
        check=True,
    )
    catalog.verify_signed_artifact(
        output.read_bytes(),
        json.loads(manifest.read_text(encoding="utf-8")),
        public.read_text(encoding="utf-8").strip(),
    )
