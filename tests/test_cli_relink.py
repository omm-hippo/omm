from typer.testing import CliRunner

from omm import cli, linker, registry

runner = CliRunner()


def test_relink_repairs_entry_missing_lmstudio_link(isolated_omm_home, monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")

    registry.save_registry(
        {
            filename: {
                "linked": {"lmstudio": False, "ollama": True},
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "ollama_name": "tinyllama",
            }
        }
    )

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    for key in ("jan", "anythingllm", "mstystudio", "textgenwebui", "koboldcpp"):
        monkeypatch.setattr(linker, f"is_{key}_installed", lambda: False)

    lmstudio_calls = []
    monkeypatch.setattr(
        linker,
        "link_lmstudio",
        lambda gguf_path, repo_id, **kwargs: lmstudio_calls.append((gguf_path, repo_id)),
    )
    monkeypatch.setattr(linker, "link_ollama", lambda gguf_path, model_name, **kwargs: True)

    result = runner.invoke(cli.app, ["relink"])

    assert result.exit_code == 0, result.stdout
    assert lmstudio_calls == [(dest, "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF")]
    updated = registry.load_registry()[filename]
    assert updated["linked"]["lmstudio"] is True
    assert updated["linked"]["ollama"] is True


def test_relink_reverifies_entry_already_marked_linked(isolated_omm_home, monkeypatch):
    """Registry says both engines are already linked - relink must still
    re-run the link so a broken/stale symlink left on disk gets repaired,
    not just entries the registry happens to flag as unlinked."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")

    registry.save_registry(
        {
            filename: {
                "linked": {"lmstudio": True, "ollama": True},
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "ollama_name": "tinyllama",
            }
        }
    )

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    for key in ("jan", "anythingllm", "mstystudio", "textgenwebui", "koboldcpp"):
        monkeypatch.setattr(linker, f"is_{key}_installed", lambda: False)

    lmstudio_calls = []
    ollama_calls = []
    monkeypatch.setattr(
        linker,
        "link_lmstudio",
        lambda gguf_path, repo_id, **kwargs: lmstudio_calls.append((gguf_path, repo_id)),
    )
    monkeypatch.setattr(
        linker,
        "link_ollama",
        lambda gguf_path, model_name, **kwargs: ollama_calls.append((gguf_path, model_name)) or True,
    )

    result = runner.invoke(cli.app, ["relink"])

    assert result.exit_code == 0, result.stdout
    assert lmstudio_calls == [(dest, "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF")]
    assert ollama_calls == [(dest, "tinyllama")]
    assert "1 model(s) relinked/verified" in result.stdout


def test_relink_counts_conflict_skip_and_marks_it_blocked(isolated_omm_home, monkeypatch):
    """A LinkError (e.g. unowned Ollama manifest) must show up in the
    summary tally instead of silently vanishing, and must persist so scan
    stops nagging to re-run `omm link` for an engine that can't succeed."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.write_bytes(b"fake-gguf")

    registry.save_registry(
        {
            filename: {
                "linked": {"ollama": False},
                "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
                "ollama_name": "tinyllama",
            }
        }
    )

    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    for key in ("lmstudio", "jan", "anythingllm", "mstystudio", "textgenwebui", "koboldcpp"):
        monkeypatch.setattr(linker, f"is_{key}_installed", lambda: False)
    monkeypatch.setattr(
        linker,
        "link_ollama",
        lambda gguf_path, model_name, **kwargs: (_ for _ in ()).throw(
            linker.LinkError("Refusing to replace unowned Ollama manifest at ...")
        ),
    )

    result = runner.invoke(cli.app, ["link"])

    assert result.exit_code == 0, result.stdout
    assert "0 model(s) relinked/verified" in result.stdout
    assert "1 skipped (conflict)" in result.stdout
    updated = registry.load_registry()[filename]
    assert updated["link_blocked"] == ["ollama"]


def test_relink_skips_entry_whose_source_file_is_missing(isolated_omm_home, monkeypatch):
    registry.save_registry({"ghost.gguf": {"linked": {"lmstudio": False, "ollama": False}}})

    monkeypatch.setattr(linker, "is_lmstudio_installed", lambda: True)
    monkeypatch.setattr(linker, "is_ollama_installed", lambda: True)
    for key in ("jan", "anythingllm", "mstystudio", "textgenwebui", "koboldcpp"):
        monkeypatch.setattr(linker, f"is_{key}_installed", lambda: False)

    result = runner.invoke(cli.app, ["relink"])

    assert result.exit_code == 0, result.stdout
    assert "0 model(s) relinked/verified" in result.stdout
    assert "1 skipped" in result.stdout


def test_relink_with_empty_registry_reports_nothing_to_do(isolated_omm_home):
    result = runner.invoke(cli.app, ["relink"])

    assert result.exit_code == 0, result.stdout
    assert "No models installed" in result.stdout
