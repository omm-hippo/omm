from unittest.mock import MagicMock
from types import SimpleNamespace

import questionary
from prompt_toolkit.input import DummyInput
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

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


def test_contribute_escape_listener_uses_win32_console_keys(monkeypatch):
    listener = __import__("omm.cli", fromlist=["_EscListener"])._EscListener()
    keys = iter([True])
    fake_msvcrt = SimpleNamespace(kbhit=lambda: next(keys), getwch=lambda: "\x1b")
    monkeypatch.setitem(__import__("sys").modules, "msvcrt", fake_msvcrt)

    listener._run_windows()

    assert listener.stop_event.is_set()
