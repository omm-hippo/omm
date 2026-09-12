import sys
import types

from omm import notify


def test_notify_calls_plyer_when_available(monkeypatch):
    calls = []
    fake_notification = types.SimpleNamespace(
        notify=lambda **kwargs: calls.append(kwargs)
    )
    fake_plyer = types.SimpleNamespace(notification=fake_notification)
    monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
    monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

    notify.notify("title", "body")

    assert calls == [
        {"title": "title", "message": "body", "app_name": "omm", "timeout": 8}
    ]


def test_notify_is_silent_when_plyer_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "plyer", None)

    notify.notify("title", "body")  # must not raise


def test_notify_is_silent_when_plyer_raises(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("no notification daemon")

    fake_notification = types.SimpleNamespace(notify=_boom)
    fake_plyer = types.SimpleNamespace(notification=fake_notification)
    monkeypatch.setitem(sys.modules, "plyer", fake_plyer)
    monkeypatch.setitem(sys.modules, "plyer.notification", fake_notification)

    notify.notify("title", "body")  # must not raise
