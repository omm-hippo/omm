import time

from omm import watch


def test_debounced_handler_fires_once_after_quiet_period(monkeypatch):
    monkeypatch.setattr(watch, "DEBOUNCE_SECONDS", 0.05)
    calls = []
    handler = watch._build_debounced_handler(lambda: calls.append(1))

    handler.on_any_event(None)
    handler.on_any_event(None)  # resets the timer - still only one eventual call

    time.sleep(0.2)
    assert calls == [1]


def test_debounced_handler_restarts_after_firing(monkeypatch):
    monkeypatch.setattr(watch, "DEBOUNCE_SECONDS", 0.05)
    calls = []
    handler = watch._build_debounced_handler(lambda: calls.append(1))

    handler.on_any_event(None)
    time.sleep(0.2)
    handler.on_any_event(None)
    time.sleep(0.2)

    assert calls == [1, 1]
