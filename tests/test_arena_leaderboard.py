import base64
from copy import deepcopy
import hashlib
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
from typer.testing import CliRunner

from omm import arena_leaderboard as board, cli, config


def model(key, tier=1, component=0, rating=0):
    return {"key": "filename:" + key, "display_filename": key + ".gguf", "repo_id": "test/" + key,
            "provider": "huggingface", "battles": 40, "effective_battles": 30, "provisional": tier is None,
            "quality_warning": False, "both_bad_rate": 0.1,
            "quality": {"tier": tier, "strength": 1, "ci_low": 0.8, "ci_high": 1.2},
            "efficiency": {"rating": rating, "component": component, "raw_median_tok_s_per_gb": 10}}


def document():
    return {"schema_version": 1, "generated_at": "2026-10-03T00:00:00+00:00",
            "corpus": {"rows_fetched": 300, "rows_used": 300, "effective_votes": 250,
                       "client_count": 20, "largest_client_share": 0.1},
            "models": [model("a", component=0, rating=1), model("b", component=1, rating=1000),
                       model("c", component=0, rating=2), model("d", tier=None)],
            "tiers": [], "efficiency_components": [], "provisional_models": ["filename:d"]}


def signed(data):
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    content = json.dumps(data).encode()
    manifest = {"schema_version": 1, "artifact_sha256": hashlib.sha256(content).hexdigest(),
                "signature": base64.b64encode(private.sign(content)).decode()}
    return content, manifest, public


def test_verified_artifact_and_group_order():
    content, manifest, public = signed(document())
    data = board.verified(content, manifest, public)
    assert [m["key"] for m in board.ordered_models(data)] == ["filename:c", "filename:a", "filename:b", "filename:d"]


def test_real_aggregation_output_round_trips_through_signed_consumer():
    from pathlib import Path
    produced = json.loads((Path(__file__).parent / "fixtures" / "arena-generated.json").read_text(encoding="utf-8"))
    assert any(model["quality"]["strength"] < 0 for model in produced["models"])
    assert board.verified(*signed(produced)) == produced


def test_tampered_artifact_and_wrong_key_are_rejected():
    content, manifest, public = signed(document())
    with pytest.raises(board.LeaderboardError):
        board.verified(content + b" ", manifest, public)
    with pytest.raises(board.LeaderboardError):
        board.verified(content, manifest, signed(document())[2])


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema_version=99),
    lambda d: d["models"][0]["quality"].update(tier=True),
    lambda d: d["models"][0].update(provisional=True),
    lambda d: d["models"][0]["quality"].update(ci_high=0),
    lambda d: d["models"][0]["efficiency"].update(rating=float("inf")),
    lambda d: d["models"].append(deepcopy(d["models"][0])),
])
def test_signed_but_invalid_schema_fails_closed(change):
    data = document()
    change(data)
    with pytest.raises(board.LeaderboardError):
        board.verified(*signed(data))


def test_cache_survives_restart_and_is_reverified_without_network(isolated_omm_home, monkeypatch):
    content, manifest, public = signed(document())
    monkeypatch.setattr(board, "_download", lambda url, maximum: content if url == board.ARTIFACT_URL else json.dumps(manifest).encode())
    data, cached = board.load(public)
    assert not cached
    monkeypatch.setattr(board, "_download", lambda *args: (_ for _ in ()).throw(AssertionError("offline request")))
    restored, cached = board.load(public, offline=True)
    assert restored == data and cached
    path = config.OMM_HOME / "arena" / "leaderboard-cache.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["content"] += " "
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(board.LeaderboardError):
        board.load(public, offline=True)


def test_cli_leaderboard_never_starts_runner_or_generates(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(board, "load", lambda *a, **k: (document(), True))
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: (_ for _ in ()).throw(AssertionError("runner must not start")))
    result = CliRunner().invoke(cli.app, ["arena", "--leaderboard", "--offline", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["verified"] is True and data["cached"] is True
    assert data["models"][-1]["provisional"] is True
    text = CliRunner().invoke(cli.app, ["arena", "--leaderboard"])
    assert "Provisional" in text.stdout and "same quality tier" in text.stdout


def test_invalid_flag_combinations_fail_before_engine(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(cli, "_ensure_engine_running", lambda *a, **k: (_ for _ in ()).throw(AssertionError("runner must not start")))
    runner = CliRunner()
    for args in (["--leaderboard", "a", "b"], ["--leaderboard", "--keep"], ["--offline"], ["--json"]):
        assert runner.invoke(cli.app, ["arena", *args]).exit_code == 2


def test_read_only_leaderboard_skips_root_flush_and_config_migration(isolated_omm_home, monkeypatch):
    monkeypatch.setattr(board, "load", lambda *a, **k: (document(), True))
    def forbidden(*a, **k):
        raise AssertionError("read-only mode must not reach mutation or upload prelude")
    monkeypatch.setattr(cli, "load_config", forbidden)
    monkeypatch.setattr(cli, "_maybe_start_update_check", forbidden)
    monkeypatch.setattr(cli.usage, "flush_pending", forbidden)
    monkeypatch.setattr(cli.telemetry, "flush_pending", forbidden)
    result = CliRunner().invoke(cli.app, ["arena", "--leaderboard", "--offline", "--json"])
    assert result.exit_code == 0, result.output
    assert not config.CONFIG_PATH.exists()
