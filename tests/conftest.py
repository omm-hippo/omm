"""Shared pytest fixtures for the omm test suite."""

from __future__ import annotations

import ipaddress
import socket

import pytest

from omm import (
    calibration,
    catalog,
    cli,
    config,
    hardware,
    linker,
    predictor,
    registry,
    scan_import,
)


def _loopback_socket_address(address: object) -> bool:
    if not isinstance(address, tuple) or not address:
        # AF_UNIX and non-INET sockets do not leave the machine.
        return True
    host = address[0]
    if not isinstance(host, str):
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_unmocked_external_network(monkeypatch):
    """Fail any unit test that attempts a real non-loopback connection.

    The suite exercises several remote-provider fallbacks. A missing mock used
    to turn an ordinary test run into a slow live ModelScope request whose
    result depended on the developer's network and consumed external service
    capacity. Local HTTP fixture servers remain available.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect(sock, address):
        if not _loopback_socket_address(address):
            raise AssertionError(f"unit test attempted external network access: {address!r}")
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        if not _loopback_socket_address(address):
            raise AssertionError(f"unit test attempted external network access: {address!r}")
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _default_remote_provider_probes_to_offline(monkeypatch):
    """Keep CLI unit tests offline unless a test supplies provider metadata.

    These names are functions imported directly into ``cli.py``, so replacing
    them here does not hide the provider modules from their own focused tests.
    Tests that exercise a positive remote-metadata path apply their own
    monkeypatch afterward and override these conservative defaults.
    """
    monkeypatch.setattr(cli, "remote_file_size", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "remote_file_sha256", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "remote_gguf_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "fetch_repo_param_count_b", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "fetch_repo_files", lambda *args, **kwargs: ([], None))


@pytest.fixture(autouse=True)
def _deterministic_terminal_capabilities(monkeypatch):
    """Keep ANSI/width tests independent of the shell that launched pytest."""
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)


@pytest.fixture(autouse=True)
def _no_real_engine_writes(tmp_path, monkeypatch):
    """Safety net for the engines that detect themselves via a fixed,
    real-home-relative directory (Jan/AnythingLLM/Msty) or a heuristic
    filesystem search (KoboldCpp/text-generation-webui): point their
    directory lookups at a throwaway tmp_path instead of the real machine,
    so a test can never silently write into (or "detect") a real local AI
    app actually installed on whatever machine runs the suite. Redirects
    the underlying directory functions rather than stubbing is_X_installed
    itself, so tests that exercise that detection logic directly still can.
    Ollama/LM Studio are untouched here - existing tests already discipline
    themselves to mock those two explicitly wherever it matters."""
    monkeypatch.setattr(linker, "jan_app_dir", lambda: tmp_path / "_no_real_jan")
    monkeypatch.setattr(linker, "anythingllm_app_dir", lambda: tmp_path / "_no_real_anythingllm")
    monkeypatch.setattr(linker, "mstystudio_app_dir", lambda: tmp_path / "_no_real_mstystudio")
    monkeypatch.setattr(
        linker, "LINK_OWNERSHIP_PATH", tmp_path / "_no_real_link_ownership.json"
    )
    monkeypatch.setattr(linker, "_HEURISTIC_SEARCH_ROOTS", [tmp_path / "_no_real_heuristic_root"])
    monkeypatch.setattr(linker, "engine_install_dir", lambda: tmp_path / "_no_real_omm_apps")
    monkeypatch.setattr(linker, "_APP_BUNDLE_SEARCH_ROOTS", [tmp_path / "_no_real_app_bundle_root"])
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()
    yield
    linker.find_koboldcpp_binary.cache_clear()
    linker.find_textgenwebui_root.cache_clear()


@pytest.fixture(autouse=True)
def _quiet_host_cpu(monkeypatch):
    """Report an idle host to every test that benchmarks something.

    ``hardware.sample_cpu_utilization_percent`` reads the real machine, so
    without this the background-load guard would decide whether a benchmark
    counts as clean based on whatever else the CI runner happens to be doing,
    and would block for the sampling window on every such test. Patching
    psutil rather than the omm function keeps the guard's own logic under
    test; tests about the guard re-patch either layer themselves and win,
    because a test's own monkeypatch is applied after this fixture."""
    monkeypatch.setattr(hardware.psutil, "cpu_percent", lambda interval=None: 1.0)


@pytest.fixture(autouse=True)
def _isolate_console_theme_state():
    """`cli.console`/`cli.err_console` are process-wide singletons, and
    `theme.apply_theme_to_console` pushes onto their rich theme stacks with
    no matching pop - so without this, any CliRunner-based test leaks its
    theme into every test that runs after it. That masked a real crash:
    a test setting `theme="no-color"` passed only because an earlier test's
    pushed theme was still on the stack underneath, so the themeless
    `no-color` codepath was never actually exercised. Also snapshots
    `no_color`, which `--no-color` flips on the same singletons.

    Reaches into `_theme_stack._entries` because rich exposes no way to
    read the current stack depth (only `push_theme`/`pop_theme`), and a
    test that popped below its starting depth couldn't be restored by
    counting pops alone."""
    consoles = (cli.console, cli.err_console)
    saved = [(c, c._theme_stack._entries[:], c.no_color) for c in consoles]
    yield
    for console, entries, no_color in saved:
        stack = console._theme_stack
        stack._entries[:] = entries
        stack.get = stack._entries[-1].get
        console.no_color = no_color


@pytest.fixture
def isolated_omm_home(tmp_path, monkeypatch):
    """Redirect all of omm's ~/.omm paths into a throwaway tmp_path so
    tests never touch (or depend on) the real user home directory."""
    home = tmp_path / ".omm"
    models_dir = home / "models"

    monkeypatch.setattr(config, "OMM_HOME", home)
    monkeypatch.setattr(config, "MODELS_DIR", models_dir)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.json")
    monkeypatch.setattr(config, "REGISTRY_PATH", home / "models.json")
    monkeypatch.setattr(config, "LINK_OWNERSHIP_PATH", home / "link-ownership.json")
    monkeypatch.setattr(config, "RULES_PATH", home / "rules.json")
    monkeypatch.setattr(config, "RECOMMEND_MODEL_PATH", home / "recommend-model.json")
    monkeypatch.setattr(config, "EVALUATIONS_DIR", home / "evaluations")
    monkeypatch.setattr(config, "CALIBRATION_PATH", home / "calibration.json")
    monkeypatch.setattr(config, "CATALOG_HISTORY_DIR", home / "catalog-history")

    monkeypatch.setattr(registry, "REGISTRY_PATH", config.REGISTRY_PATH)
    monkeypatch.setattr(linker, "LINK_OWNERSHIP_PATH", config.LINK_OWNERSHIP_PATH)
    monkeypatch.setattr(linker, "MODELS_DIR", models_dir)
    monkeypatch.setattr(cli, "MODELS_DIR", models_dir)
    monkeypatch.setattr(scan_import, "MODELS_DIR", models_dir)
    monkeypatch.setattr(predictor, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(calibration, "CALIBRATION_PATH", config.CALIBRATION_PATH)
    monkeypatch.setattr(catalog, "RECOMMEND_MODEL_PATH", config.RECOMMEND_MODEL_PATH)
    monkeypatch.setattr(catalog, "CATALOG_HISTORY_DIR", config.CATALOG_HISTORY_DIR)

    config.ensure_omm_home()
    return home
