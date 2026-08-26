from unittest.mock import MagicMock
from types import SimpleNamespace

import prompt_toolkit
import pytest
import questionary
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from omm import cli
from omm.cli import _add_escape_to_cancel


def test_escape_binding_triggers_keyboard_interrupt_style_exit():
    # DummyInput/DummyOutput: constructing a real Question tries to open a
    # console, which CI runners (esp. Windows, with stdout captured by
    # pytest) don't have.
    question = questionary.select(
        "Pick one:",
        choices=[questionary.Choice(title="a", value="a")],
        input=DummyInput(),
        output=DummyOutput(),
    )

    _add_escape_to_cancel(question)

    escape_bindings = [
        b for b in question.application.key_bindings.bindings if b.keys == (Keys.Escape,)
    ]
    assert escape_bindings, "expected an Escape key binding to be registered"

    fake_event = MagicMock()
    escape_bindings[-1].handler(fake_event)

    fake_event.app.exit.assert_called_once_with(
        exception=KeyboardInterrupt, style="class:aborting"
    )


def _fake_ctypes_for_windows_esc_listener(*, foreground_window=1, console_window=1):
    class FakeGetAsyncKeyState:
        argtypes = None
        restype = None

        def __call__(self, key):
            assert key == 0x1B
            return 0x8000

    return SimpleNamespace(
        windll=SimpleNamespace(
            user32=SimpleNamespace(
                GetAsyncKeyState=FakeGetAsyncKeyState(),
                GetForegroundWindow=lambda: foreground_window,
            ),
            kernel32=SimpleNamespace(GetConsoleWindow=lambda: console_window),
        ),
        c_int=int,
        c_short=int,
    )


def test_contribute_escape_listener_polls_without_consuming_console_input(monkeypatch):
    listener = __import__("omm.cli", fromlist=["_EscListener"])._EscListener()
    fake_ctypes = _fake_ctypes_for_windows_esc_listener(foreground_window=1, console_window=1)
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)

    listener._run_windows()

    assert listener.stop_event.is_set()


def test_contribute_escape_listener_ignores_escape_when_console_not_focused(monkeypatch):
    """GetAsyncKeyState reports global OS-wide key state - without a focus
    check, pressing Escape while any other window has focus (e.g.
    alt-tabbed away during a long benchmark) would wrongly abort
    `omm contribute`. A false positive here would `return` before ever
    calling `time.sleep`, so counting sleep calls distinguishes "correctly
    ignored, loop kept polling" from "wrongly registered, loop exited
    immediately"."""
    from omm import cli

    listener = cli._EscListener()
    fake_ctypes = _fake_ctypes_for_windows_esc_listener(foreground_window=1, console_window=2)
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)
    sleep_calls = {"n": 0}

    def fake_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            listener.stop_event.set()

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)

    listener._run_windows()

    assert sleep_calls["n"] == 3


def test_ask_single_key_marks_foreground_prompt_active_during_run(monkeypatch):
    """_EscListener's background thread must not read stdin while a
    foreground y/n/a prompt owns it, or the two readers race for the same
    keystrokes (see _FOREGROUND_PROMPT_ACTIVE's docstring). This checks the
    prompt side of that contract: the flag is set for the duration of the
    prompt's own event loop and cleared once it returns."""
    seen: dict[str, bool] = {}

    class FakeApp:
        def run(self):
            seen["active_during_run"] = cli._FOREGROUND_PROMPT_ACTIVE.is_set()
            return True

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.app = FakeApp()

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    monkeypatch.setattr(cli, "_require_tty", lambda *_: None)
    assert not cli._FOREGROUND_PROMPT_ACTIVE.is_set()

    result = cli._ask_single_key(
        "msg", [("y", "Yes", True)], default_value=False, instruction="(y)"
    )

    assert result is True
    assert seen["active_during_run"] is True
    assert not cli._FOREGROUND_PROMPT_ACTIVE.is_set()


def test_ask_single_key_clears_foreground_prompt_flag_on_exception(monkeypatch):
    class FakeApp:
        def run(self):
            raise KeyboardInterrupt

    class FakeSession:
        def __init__(self, *args, **kwargs):
            self.app = FakeApp()

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakeSession)
    monkeypatch.setattr(cli, "_require_tty", lambda *_: None)

    with pytest.raises(KeyboardInterrupt):
        cli._ask_single_key(
            "msg", [("y", "Yes", True)], default_value=False, instruction="(y)"
        )

    assert not cli._FOREGROUND_PROMPT_ACTIVE.is_set()


def test_esc_listener_posix_skips_reading_while_foreground_prompt_active(monkeypatch):
    """Reproduces the race: without the flag check, this loop would call
    select()/read_keys() concurrently with a foreground prompt's own stdin
    read, stealing keystrokes meant for y/n/a. With the fix, the listener
    must not touch select at all while the flag is set."""

    class FakeRawMode:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeInput:
        def fileno(self):
            return 0

        def raw_mode(self):
            return FakeRawMode()

        def read_keys(self):
            return []

    listener = cli._EscListener()
    calls = {"select": 0, "sleep": 0}

    def fake_select(rlist, wlist, xlist, timeout):
        calls["select"] += 1
        return ([], [], [])

    def fake_sleep(seconds):
        calls["sleep"] += 1
        if calls["sleep"] >= 3:
            listener.stop_event.set()

    monkeypatch.setattr("prompt_toolkit.input.create_input", lambda: FakeInput())
    monkeypatch.setattr("select.select", fake_select)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    monkeypatch.setattr("sys.platform", "darwin")
    cli._FOREGROUND_PROMPT_ACTIVE.set()
    try:
        listener._run_posix()
    finally:
        cli._FOREGROUND_PROMPT_ACTIVE.clear()

    assert calls["select"] == 0
    assert calls["sleep"] == 3
