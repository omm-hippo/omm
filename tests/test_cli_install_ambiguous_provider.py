"""Tests that `omm install org/repo` prompts for a provider when the repo
exists on more than one, instead of crashing or silently picking one."""

from __future__ import annotations

import pytest
import questionary

from omm import cli
from omm.hub import AmbiguousProviderError, ResolvedModel


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

    cli.install("org/repo", skip_unfit=False, upload=None)

    assert calls == ["modelscope:org/repo"]
