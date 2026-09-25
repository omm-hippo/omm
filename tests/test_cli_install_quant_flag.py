"""`omm install <ref> --quant NAME` skips the quant picker (issue #387)."""

import pytest
from typer.testing import CliRunner

from omm import cli
from omm.hub import AmbiguousModelError, ResolvedModel

runner = CliRunner()

REPO_ID = "TheBloke/Llama-2-7B-GGUF"
CANDIDATES = [
    "llama-2-7b.Q2_K.gguf",
    "llama-2-7b.Q4_K_M.gguf",
    "llama-2-7b.Q8_0.gguf",
]


@pytest.fixture
def stubbed(isolated_omm_home, monkeypatch):
    calls = {"resolve": [], "install_impl": []}

    def fake_resolve(name):
        calls["resolve"].append(name)
        if name == REPO_ID:
            raise AmbiguousModelError(REPO_ID, CANDIDATES)
        filename = name.rsplit(":", 1)[-1]
        return ResolvedModel(
            url=f"https://example.com/{filename}", filename=filename, repo_id=REPO_ID
        )

    def fake_install_impl(resolved, **kwargs):
        calls["install_impl"].append(resolved.filename)
        return cli.InstallOutcome(filename=resolved.filename, repo_id=resolved.repo_id, linked={})

    def picker_must_not_run(*_a, **_k):
        raise AssertionError("quant picker must not open when --quant is given")

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)
    monkeypatch.setattr(cli, "_install_plan_for", lambda resolved: None)
    monkeypatch.setattr(cli, "_pick_quant_variant", picker_must_not_run)
    monkeypatch.setattr(cli, "_ask_select", picker_must_not_run)
    return calls


def test_quant_flag_picks_matching_file_without_picker(stubbed):
    result = runner.invoke(cli.app, ["install", REPO_ID, "--quant", "Q4_K_M"])

    assert result.exit_code == 0, result.output
    assert stubbed["resolve"] == [REPO_ID, f"huggingface:{REPO_ID}:llama-2-7b.Q4_K_M.gguf"]
    assert stubbed["install_impl"] == ["llama-2-7b.Q4_K_M.gguf"]


@pytest.mark.parametrize("spelling", ["q4_k_m", "Q4-K-M", "q8_0"])
def test_quant_flag_match_is_case_and_separator_insensitive(stubbed, spelling):
    result = runner.invoke(cli.app, ["install", REPO_ID, "--quant", spelling])

    assert result.exit_code == 0, result.output
    expected = "llama-2-7b.Q8_0.gguf" if spelling == "q8_0" else "llama-2-7b.Q4_K_M.gguf"
    assert stubbed["install_impl"] == [expected]


def test_quant_flag_without_match_lists_available_quants(stubbed):
    result = runner.invoke(cli.app, ["install", REPO_ID, "--quant", "Q5_K_S"])

    assert result.exit_code == 1
    assert "No 'Q5_K_S' quant" in result.stderr
    assert "Available quants: Q2_K, Q4_K_M, Q8_0" in " ".join(result.stderr.split())
    assert stubbed["install_impl"] == []


def test_quant_flag_with_several_files_for_one_quant_refuses_to_guess(
    isolated_omm_home, monkeypatch
):
    candidates = ["llama-2-7b-chat.Q4_K_M.gguf", "llama-2-7b.Q4_K_M.gguf"]
    monkeypatch.setattr(
        cli,
        "resolve_model",
        lambda name: (_ for _ in ()).throw(AmbiguousModelError(REPO_ID, candidates)),
    )
    monkeypatch.setattr(cli, "_pick_quant_variant", lambda e: pytest.fail("picker opened"))

    result = runner.invoke(cli.app, ["install", REPO_ID, "--quant", "q4_k_m"])

    assert result.exit_code == 1
    assert "has 2 files for quant 'q4_k_m'" in " ".join(result.stderr.split())
    assert "llama-2-7b-chat.Q4_K_M.gguf" in " ".join(result.stderr.split())


def test_quant_flag_checks_an_already_single_file_ref(stubbed):
    ref = f"{REPO_ID}:llama-2-7b.Q8_0.gguf"

    ok = runner.invoke(cli.app, ["install", ref, "--quant", "Q8_0"])
    mismatch = runner.invoke(cli.app, ["install", ref, "--quant", "Q4_K_M"])

    assert ok.exit_code == 0, ok.output
    assert mismatch.exit_code == 1
    assert "Available quants: Q8_0" in " ".join(mismatch.stderr.split())
    assert stubbed["install_impl"] == ["llama-2-7b.Q8_0.gguf"]


def test_without_quant_flag_the_picker_still_runs(isolated_omm_home, monkeypatch):
    picked = []

    def fake_resolve(name):
        if name == REPO_ID:
            raise AmbiguousModelError(REPO_ID, CANDIDATES)
        raise AssertionError("cancelled picker must not resolve further")

    monkeypatch.setattr(cli, "resolve_model", fake_resolve)
    monkeypatch.setattr(cli, "_pick_quant_variant", lambda e: picked.append(e) or None)

    result = runner.invoke(cli.app, ["install", REPO_ID])

    assert result.exit_code == 0
    assert len(picked) == 1
    assert "Cancelled" in result.stderr
