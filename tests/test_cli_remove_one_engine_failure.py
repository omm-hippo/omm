"""Regression coverage for `_remove_one` forgetting a model whose engine
cleanup failed.

`linker.py`'s engine unlink helpers (e.g. `unlink_ollama`) were changed to
raise `linker.LinkError` on a lock timeout or permission failure instead of
silently swallowing it and returning False. `_remove_one` already catches
that `LinkError` per engine and prints a warning, but once the central hub
file itself was successfully deleted it still unconditionally called
`registry.remove_entry(filename)` - forgetting the model existed at all and
leaving a zombie manifest/blob at the still-linked engine with no recorded
owner and no way to retry via `omm relink` / `omm uninstall` again.

This exercises the real `_remove_one` (nothing about it is stubbed) against
a real, isolated registry - only the engine-facing `linker.unlink_engine`
call and the Ollama daemon reachability check are faked.
"""

from omm import cli, linker, registry


def test_remove_one_keeps_registry_entry_when_engine_cleanup_fails(monkeypatch):
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-gguf")

    entry = {
        "linked": {"ollama": True, "lmstudio": True},
        "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "ollama_name": "tinyllama",
        "sha256": "deadbeef",
    }
    registry.save_registry({filename: entry})

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)

    def fake_unlink_engine(key, fname, ent, **kwargs):
        if key == "lmstudio":
            raise linker.LinkError("LM Studio manifest is locked by another process")
        return None

    monkeypatch.setattr(linker, "unlink_engine", fake_unlink_engine)

    result = cli._remove_one(filename, entry, ollama_tag="tinyllama")

    assert result is False
    # The hub file removal itself is independent of engine cleanup and must
    # still succeed exactly as before this fix.
    assert not dest.exists()

    updated = registry.load_registry().get(filename)
    assert updated is not None, (
        "the registry entry must not be forgotten while lmstudio cleanup is "
        "unresolved - a zombie manifest/blob needs a recorded owner to retry"
    )
    assert updated["linked"]["ollama"] is False
    assert updated["linked"]["lmstudio"] is True
    assert updated["sha256"] == "deadbeef"


def test_remove_one_still_forgets_entry_when_every_engine_cleans_up(monkeypatch):
    """Sanity check: the ordinary success path (no LinkError at all) is
    unchanged - the registry entry is dropped once everything is clean."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-gguf")

    entry = {
        "linked": {"ollama": True},
        "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
        "ollama_name": "tinyllama",
    }
    registry.save_registry({filename: entry})

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(linker, "unlink_engine", lambda *a, **k: None)

    result = cli._remove_one(filename, entry, ollama_tag="tinyllama")

    assert result is True
    assert not dest.exists()
    assert registry.load_registry().get(filename) is None


def test_remove_one_unloads_lmstudio_before_unlinking(monkeypatch):
    """Regression (audit #6, part 1): `_remove_one` unloaded Ollama before
    unlinking but left LM Studio (and every other engine) loaded, so
    unlinking a file LM Studio still had open could hit a Windows sharing
    violation. It must resolve the linked model's LM Studio model key and
    ask LM Studio to unload it first, the same way it already does for
    Ollama."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-gguf")

    entry = {
        "linked": {"lmstudio": True},
        "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
    }
    registry.save_registry({filename: entry})

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(linker, "unlink_engine", lambda *a, **k: None)
    monkeypatch.setattr(
        linker,
        "resolve_lmstudio_model",
        lambda repo_id, fname: {"model_key": "thebloke/tinyllama-1.1b-chat-v1.0"},
    )
    unload_calls = []
    monkeypatch.setattr(
        cli.quality_mod,
        "unload_model",
        lambda tag, engine="ollama": unload_calls.append((tag, engine)) or True,
    )

    result = cli._remove_one(filename, entry, ollama_tag="tinyllama")

    assert result is True
    assert unload_calls == [("thebloke/tinyllama-1.1b-chat-v1.0", "lmstudio")]


def test_remove_one_survives_permission_error_from_engine_unlink(monkeypatch):
    """Regression (audit #6, part 2): `_unlink_owned_link_with_retry`
    (the Windows sharing-violation retry loop LM Studio's/custom apps'
    unlink path goes through) re-raises a bare `PermissionError` once its
    retries run out - not a `linker.LinkError`. Catching only `LinkError`
    let that crash `_remove_one` outright instead of skipping just this
    engine, which - via `uninstall all`'s per-model loop - would also skip
    the model-file removal for every model still queued behind this one
    and the batched `pending_ollama_unlinks.flush()`."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-gguf")

    entry = {
        "linked": {"lmstudio": True},
        "repo_id": "TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF",
    }
    registry.save_registry({filename: entry})

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(linker, "resolve_lmstudio_model", lambda *a, **k: None)

    def fake_unlink_engine(key, fname, ent, **kwargs):
        raise PermissionError("[WinError 32] file is in use by LM Studio")

    monkeypatch.setattr(linker, "unlink_engine", fake_unlink_engine)

    # Must not raise.
    result = cli._remove_one(filename, entry, ollama_tag="tinyllama")

    assert result is False
    # The hub file removal still ran despite the engine-cleanup failure.
    assert not dest.exists()
    updated = registry.load_registry().get(filename)
    assert updated is not None
    assert updated["linked"]["lmstudio"] is True


def test_remove_one_survives_permission_error_from_custom_link_cleanup(monkeypatch):
    """Companion to the above for `omm link <directory>` destinations: that
    cleanup call had no try/except at all, so a Windows sharing violation
    there crashed `_remove_one` even more directly."""
    filename = "tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
    dest = cli.MODELS_DIR / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"fake-gguf")

    entry = {
        "linked": {},
        "custom_links": ["/some/custom/dir/model.gguf"],
    }
    registry.save_registry({filename: entry})

    monkeypatch.setattr(cli.benchmark, "ollama_daemon_reachable", lambda: False)
    monkeypatch.setattr(
        linker,
        "_unlink_owned_link_with_retry",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("still open")),
    )

    # Must not raise.
    result = cli._remove_one(filename, entry, ollama_tag="tinyllama")

    assert result is True  # no engine was linked, so this model still fully uninstalls
    assert not dest.exists()
    assert registry.load_registry().get(filename) is None
