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
    assert artifact["schema_version"] == 1
    assert artifact["models"]
    assert "gate_status=passed" in outputs.read_text(encoding="utf-8")
    assert "artifact_changed=true" in outputs.read_text(encoding="utf-8")


def test_a_blocked_run_exits_zero_and_writes_nothing(tmp_path, capsys):
    votes = tmp_path / "votes.jsonl"
    votes.write_text(json.dumps(_row(0, "aa", "a")), encoding="utf-8")
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
            "5",
        ]
    )
    assert exit_code == 0
    assert not output.exists()
    assert "gate_status=blocked" in outputs.read_text(encoding="utf-8")
    assert "effective votes" in capsys.readouterr().out


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
