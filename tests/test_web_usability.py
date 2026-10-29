from __future__ import annotations

import errno
from pathlib import Path
import socket
from types import SimpleNamespace
import uuid

import pytest

from omm import install_safety, predictor, recommend_selection, recommend_status, rules, hub
from omm.hardware import HardwareInfo
from omm.web import runtime
from omm.web.jobs import JobManager
from omm.web.jobs import JobConflict
from omm.web.recovery import ActionError, failure
from omm.web.service import WebService


@pytest.fixture
def startup(monkeypatch, isolated_omm_home):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv('OLLAMA_HOST', f'127.0.0.1:{port}')
    calls = []
    proc = SimpleNamespace(pid=12345, poll=lambda: None)
    health = iter([False, True])
    monkeypatch.setattr(runtime, 'OllamaAdapter', lambda endpoint: SimpleNamespace(
        health=lambda: SimpleNamespace(reachable=next(health, True), version='test')))
    monkeypatch.setattr(runtime.linker, 'find_ollama_executable', lambda: Path('/reviewed/ollama'))
    monkeypatch.setattr(runtime.install_safety, 'verify_installed', lambda key: calls.append('scan'))
    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))
        return proc
    monkeypatch.setattr(runtime.subprocess, 'Popen', spawn)
    monkeypatch.setattr(runtime.engine_security, 'record_owned_ollama', lambda *args: True)
    monkeypatch.setattr(runtime.benchmark, 'stop_ollama_daemon', lambda value: calls.append(('stop', value)))
    return calls, proc, port


def test_start_scans_before_launch_and_owns_only_its_handle(startup):
    calls, proc, port = startup
    manager = runtime.RuntimeManager()
    result = manager.start()
    assert result['server_ready'] and result['owned'] and not result['reused']
    assert calls[0] == 'scan'
    argv, kwargs = calls[1]
    assert argv == [str(Path('/reviewed/ollama')), 'serve']
    assert kwargs['env']['OLLAMA_HOST'] == f'127.0.0.1:{port}'
    assert 'shell' not in kwargs
    assert manager.start()['reused'] is True
    assert len(calls) == 2
    manager.close()
    manager.close()
    assert calls[2:] == [('stop', proc)]


def test_borrowed_server_is_not_scanned_spawned_or_stopped(startup, monkeypatch):
    calls, _, _ = startup
    monkeypatch.setattr(runtime, 'OllamaAdapter', lambda _: SimpleNamespace(
        health=lambda: SimpleNamespace(reachable=True, version='existing')))
    manager = runtime.RuntimeManager()
    result = manager.start()
    assert result['reused'] and not result['owned']
    manager.close()
    assert calls == []


def test_failed_safety_never_launches(startup, monkeypatch):
    calls, _, _ = startup
    def reject(_):
        raise install_safety.SafetyError('blocked')
    monkeypatch.setattr(runtime.install_safety, 'verify_installed', reject)
    with pytest.raises(install_safety.SafetyError):
        runtime.RuntimeManager().start()
    assert not calls


@pytest.mark.parametrize('endpoint', ['http://0.0.0.0:11434', 'http://192.168.1.2:11434',
                                   'http://user:pass@localhost:11434', 'http://localhost:11434/api',
                                   'https://localhost:11434', 'http://[::1]:11434', 'http://localhost:0', 'http://localhost'])
def test_unapproved_bind_cannot_start(startup, monkeypatch, endpoint):
    calls, _, _ = startup
    monkeypatch.setenv('OLLAMA_HOST', endpoint)
    with pytest.raises(ValueError):
        runtime.RuntimeManager().start()
    assert not calls


def test_occupied_port_does_not_kill_or_spawn(startup):
    calls, _, port = startup
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))
        sock.listen()
        with pytest.raises(ActionError, match='다른 프로그램'):
            runtime.RuntimeManager().start()
    assert calls == ['scan']


def test_failed_launch_cleans_only_created_handle(startup):
    calls, proc, _ = startup
    manager = runtime.RuntimeManager()
    with pytest.raises(ActionError):
        manager.start(timeout=0)
    assert calls[-1] == ('stop', proc)
    assert manager.process is None


def test_server_start_is_serialized_idempotent_and_durable(startup):
    manager = JobManager()
    manager.runtime_manager = runtime.RuntimeManager()
    def active_chat():
        raise JobConflict('active chat')
    manager.operation_guard = active_chat
    key = str(uuid.uuid4())
    request = WebService().request({'operation': 'engine_start', 'engine': 'ollama', 'confirmed': True})
    try:
        manager.start(request, key)
        worker = manager.workers.get(key)
        if worker:
            worker.join(5)
        assert manager.list()[0]['status'] == 'completed'
        assert manager.start(request, key)['result']['server_ready']
        with pytest.raises(JobConflict, match='active chat'):
            manager.start({'operation': 'link', 'filename': 'tiny.gguf'}, str(uuid.uuid4()))
    finally:
        manager.close()
        manager.runtime_manager.close()
    restarted = JobManager()
    try:
        assert restarted.list()[0]['result']['server_ready']
    finally:
        restarted.close()


@pytest.mark.parametrize('body', [
    {'engine': 'lmstudio', 'confirmed': True}, {'engine': 'ollama', 'confirmed': False},
    {'engine': 'ollama', 'confirmed': True, 'command': 'open -a Ollama'},
    {'engine': 'ollama', 'confirmed': True, 'ids': ['arbitrary']},
])
def test_start_request_rejects_unconfirmed_or_extra_inputs(body):
    with pytest.raises(ValueError):
        WebService().request({'operation': 'engine_start', **body})


def test_only_typed_reasons_drive_recovery():
    assert failure(ValueError('out of memory disk full server_unavailable')) == {'reason': 'unknown'}
    assert failure(OSError(errno.ENOSPC, 'no space')) == {'reason': 'disk_full'}
    assert failure(OSError(errno.EACCES, 'denied')) == {'reason': 'unknown'}
    assert failure(install_safety.SafetyError('blocked')) == {'reason': 'safety_failed'}
    assert failure(ActionError('out_of_memory', '')) == {'reason': 'out_of_memory'}
    assert failure(ActionError([], '')) == {'reason': 'unknown'}
