from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from omm import cli, config, recommend_ui, search as search_mod

runner = CliRunner()

TRUSTED = {"repo_id": "bartowski/Qwen3-8B-GGUF", "provider": "huggingface"}
UNTRUSTED = {"repo_id": "random-person/Qwen3-8B-GGUF", "provider": "huggingface"}


# --- allowlist ---------------------------------------------------------------


def test_allowlisted_owner_is_trusted_case_insensitively():
    assert search_mod.is_trusted_uploader(TRUSTED)
    assert search_mod.is_trusted_uploader({"repo_id": "TheBloke/X-GGUF"})  # default provider HF
    assert search_mod.is_trusted_uploader({"repo_id": "QWEN/Qwen3-8B-GGUF"})


def test_unknown_owner_is_not_trusted():
    assert not search_mod.is_trusted_uploader(UNTRUSTED)


def test_trust_does_not_cross_providers():
    # LLM-Research is ModelScope's own mirror org; the same name on
    # HuggingFace could belong to anyone.
    assert search_mod.is_trusted_uploader({"repo_id": "LLM-Research/x", "provider": "modelscope"})
    assert not search_mod.is_trusted_uploader({"repo_id": "LLM-Research/x", "provider": "huggingface"})


def test_curated_defaults_are_always_trusted():
    name = next(iter(search_mod.hub.CURATED_INDEX))
    assert search_mod.is_trusted_uploader({"name": name, "repo_id": "whoever/x"})
    assert search_mod.is_trusted_uploader({"repo_id": "whoever/x", "description": "Curated default"})


def test_candidate_without_repo_id_is_not_trusted():
    assert not search_mod.is_trusted_uploader({"name": "direct.gguf"})
    assert not search_mod.is_trusted_uploader({"repo_id": "no-slash"})


# --- policy application -------------------------------------------------------


def test_warn_flags_untrusted_without_mutating_inputs():
    untrusted = dict(UNTRUSTED)
    guarded, hidden = search_mod.apply_uploader_guard([TRUSTED, untrusted], "warn")

    assert hidden == 0
    assert [c["repo_id"] for c in guarded] == [TRUSTED["repo_id"], UNTRUSTED["repo_id"]]
    assert "uploader_unverified" not in guarded[0]
    assert guarded[1]["uploader_unverified"] is True
    assert "uploader_unverified" not in untrusted


def test_strict_hides_untrusted_and_counts_them():
    guarded, hidden = search_mod.apply_uploader_guard([TRUSTED, UNTRUSTED, UNTRUSTED], "strict")
    assert guarded == [TRUSTED]
    assert hidden == 2


def test_off_passes_everything_through_unflagged():
    guarded, hidden = search_mod.apply_uploader_guard([TRUSTED, UNTRUSTED], "off")
    assert guarded == [TRUSTED, UNTRUSTED]
    assert hidden == 0


@pytest.mark.parametrize("bad", [None, "", "OFFF", 3, ["off"]])
def test_damaged_policy_falls_back_to_warn_never_off(bad):
    assert search_mod.normalize_uploader_guard_policy(bad) == "warn"
    guarded, _ = search_mod.apply_uploader_guard([UNTRUSTED], bad)
    assert guarded[0]["uploader_unverified"] is True


def test_config_defaults_to_warn_and_repairs_damaged_value(isolated_omm_home):
    assert config.load_config()["uploader_guard_policy"] == "warn"
    config.update_config(uploader_guard_policy="nonsense")
    assert config.load_config()["uploader_guard_policy"] == "warn"


# --- recommend row ------------------------------------------------------------


def test_recommend_row_warning_names_unverified_uploader():
    assert recommend_ui._warning({**UNTRUSTED, "uploader_unverified": True}) == "Unverified uploader"
    assert recommend_ui._warning(dict(TRUSTED)) is None


# --- omm setting uploader-guard -----------------------------------------------


def test_setting_command_saves_policy(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "uploader-guard", "--policy", "STRICT"])

    assert result.exit_code == 0, result.output
    assert config.load_config()["uploader_guard_policy"] == "strict"
    assert "strict" in result.output


def test_setting_command_rejects_unknown_policy(isolated_omm_home):
    result = runner.invoke(cli.app, ["setting", "uploader-guard", "--policy", "lenient"])

    assert result.exit_code == 1
    assert config.load_config()["uploader_guard_policy"] == "warn"


# --- omm search -----------------------------------------------------------------


def _search_setup(monkeypatch, policy):
    monkeypatch.setattr(
        cli, "load_config", lambda: {"model_url": None, "uploader_guard_policy": policy}
    )
    monkeypatch.setattr(cli.search_mod, "local_candidate_pool", lambda model_url, **kwargs: [])
    monkeypatch.setattr(
        cli.search_mod,
        "search_huggingface",
        lambda query, **kwargs: [
            {"name": r["repo_id"], "repo_id": r["repo_id"], "filename": "m.Q4_K_M.gguf",
             "description": "HuggingFace", "provider": "huggingface"}
            for r in (TRUSTED, UNTRUSTED)
        ],
    )
    monkeypatch.setattr(cli.search_mod, "search_modelscope", lambda query, **kwargs: [])
    monkeypatch.setattr(cli.session_cache, "record_results", lambda refs: None)


def test_search_warn_flags_unverified_rows(monkeypatch):
    _search_setup(monkeypatch, "warn")

    result = runner.invoke(cli.app, ["search", "qwen3"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    untrusted_line = next(line for line in lines if UNTRUSTED["repo_id"] in line)
    trusted_line = next(line for line in lines if TRUSTED["repo_id"] in line)
    assert "(unverified uploader)" in untrusted_line
    assert "(unverified uploader)" not in trusted_line


def test_search_strict_hides_unverified_rows(monkeypatch):
    _search_setup(monkeypatch, "strict")

    result = runner.invoke(cli.app, ["search", "qwen3"])

    assert result.exit_code == 0, result.output
    assert TRUSTED["repo_id"] in result.stdout
    assert UNTRUSTED["repo_id"] not in result.stdout
    assert "hid 1 model" in result.output


def test_search_json_reports_trust_even_when_guard_is_off(monkeypatch):
    _search_setup(monkeypatch, "off")

    result = runner.invoke(cli.app, ["search", "qwen3", "--json"])

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    by_ref = {row["ref"]: row["trusted_uploader"] for row in rows}
    assert by_ref == {TRUSTED["repo_id"]: True, UNTRUSTED["repo_id"]: False}


# --- install by repo id -------------------------------------------------------------


def _install_setup(monkeypatch, policy, *, confirm=None):
    monkeypatch.setattr(cli, "load_config", lambda: {"uploader_guard_policy": policy})
    asked = []

    def fake_confirm(message, default=False):
        asked.append(message)
        return confirm

    monkeypatch.setattr(cli, "_ask_confirm", fake_confirm)
    return asked


def test_install_warn_prints_one_line_and_continues(monkeypatch, capsys):
    asked = _install_setup(monkeypatch, "warn")

    cli._check_install_uploader(UNTRUSTED["repo_id"], "huggingface", assume_yes=False)

    assert asked == []
    assert "random-person is not on OMM's trusted uploader list" in capsys.readouterr().err


def test_install_trusted_and_off_are_silent(monkeypatch, capsys):
    _install_setup(monkeypatch, "strict")
    cli._check_install_uploader(TRUSTED["repo_id"], "huggingface", assume_yes=False)
    _install_setup(monkeypatch, "off")
    cli._check_install_uploader(UNTRUSTED["repo_id"], "huggingface", assume_yes=False)

    assert capsys.readouterr().err == ""


def test_install_strict_declined_cancels(monkeypatch):
    asked = _install_setup(monkeypatch, "strict", confirm=False)

    with pytest.raises(typer.Exit) as exit_info:
        cli._check_install_uploader(UNTRUSTED["repo_id"], "huggingface", assume_yes=False)

    assert exit_info.value.exit_code == 0
    assert asked == ["Install anyway?"]


def test_install_strict_with_yes_skips_prompt(monkeypatch):
    asked = _install_setup(monkeypatch, "strict", confirm=False)

    cli._check_install_uploader(UNTRUSTED["repo_id"], "huggingface", assume_yes=True)

    assert asked == []
