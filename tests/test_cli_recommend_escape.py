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


def test_contribute_escape_listener_polls_without_consuming_console_input(monkeypatch):
    listener = __import__("omm.cli", fromlist=["_EscListener"])._EscListener()

    class FakeGetAsyncKeyState:
        argtypes = None
        restype = None

        def __call__(self, key):
            assert key == 0x1B
            return 0x8000

    fake_ctypes = SimpleNamespace(
        windll=SimpleNamespace(user32=SimpleNamespace(GetAsyncKeyState=FakeGetAsyncKeyState())),
        c_int=int,
        c_short=int,
    )
    monkeypatch.setitem(__import__("sys").modules, "ctypes", fake_ctypes)

    listener._run_windows()

    assert listener.stop_event.is_set()
