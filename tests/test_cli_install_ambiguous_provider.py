"""Tests that `omm install org/repo` prompts for a provider when the repo
exists on more than one, instead of crashing or silently picking one."""

from __future__ import annotations

import questionary
from typer.testing import CliRunner

from omm import cli
from omm.hub import AmbiguousProviderError, ResolvedModel

runner = CliRunner()


def test_install_prompts_for_provider_on_ambiguous_match(monkeypatch, isolated_omm_home):
    calls = []

    def fake_resolve_model(name):
        if name == "org/repo":
            raise AmbiguousProviderError("org/repo", ["huggingface", "modelscope"])
        calls.append(name)
        return ResolvedModel(
            url="https://modelscope.cn/api/v1/models/org/repo/repo?FilePath=x.gguf",
            filename="x.gguf",
            repo_id="org/repo",
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(cli, "_resolve_ref", lambda name: name)
    # questionary.select(...) is evaluated eagerly as an argument to
    # _ask_select, so it must be stubbed too - constructing a real Question
    # tries to open a console, which CI runners (esp. Windows) don't have.
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "_ask_select", lambda prompt: "modelscope"
    )
    monkeypatch.setattr(
        cli,
        "_install_impl",
        lambda resolved, **kwargs: cli.InstallOutcome(
            resolved.filename, resolved.repo_id, {}, None, None, False, sha256="x"
        ),
    )

    result = runner.invoke(cli.app, ["install", "org/repo"])

    assert result.exit_code == 0, result.stdout
    assert calls == ["modelscope:org/repo"]


def test_install_force_flag_survives_ambiguous_provider_recursion(monkeypatch, isolated_omm_home):
    # Same re-entry concern as the ambiguous-quant path: install() calls
    # itself once the provider is picked, and --force has to survive that
    # recursive call to reach _install_impl (see #81).
    def fake_resolve_model(name):
        if name == "org/repo":
            raise AmbiguousProviderError("org/repo", ["huggingface", "modelscope"])
        return ResolvedModel(
            url="https://modelscope.cn/api/v1/models/org/repo/repo?FilePath=x.gguf",
            filename="x.gguf",
            repo_id="org/repo",
            provider="modelscope",
        )

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(cli, "_resolve_ref", lambda name: name)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda prompt: "modelscope")

    install_impl_calls = []

    def fake_install_impl(resolved, **kwargs):
        install_impl_calls.append(kwargs)
        return cli.InstallOutcome(resolved.filename, resolved.repo_id, {}, None, None, False, sha256="x")

    monkeypatch.setattr(cli, "_install_impl", fake_install_impl)

    result = runner.invoke(cli.app, ["install", "org/repo", "--force"])

    assert result.exit_code == 0, result.stdout
    assert len(install_impl_calls) == 1
    assert install_impl_calls[0]["force"] is True


def test_install_cancels_cleanly_when_provider_prompt_is_escaped(monkeypatch, isolated_omm_home):
    repo_id = "org/repo"

    def fake_resolve_model(name):
        raise AmbiguousProviderError(repo_id, ["huggingface", "modelscope"])

    monkeypatch.setattr(cli, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(cli, "_resolve_ref", lambda name: name)
    monkeypatch.setattr(questionary, "select", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_ask_select", lambda prompt: None)

    result = runner.invoke(cli.app, ["install", repo_id])

    assert result.exit_code == 0
    assert "Cancelled" in result.stderr
