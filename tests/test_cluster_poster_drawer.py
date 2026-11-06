"""The drawer child process: one JSON request per line in, one JSON reply per line out.

A fake drawer script stands in for mflux; it speaks the same protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

import pytest

from omm.cluster import poster_drawer
from omm.cluster.poster_drawer import Drawer, DrawerError, DrawerTimeout

FAKE = r'''
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
print(json.dumps({"ready": True, "seconds": 0.01}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["FAKE_SEEN"], "a") as seen:
        seen.write(json.dumps({"request": request, "argv": sys.argv}) + "\n")
    if mode == "slow":
        time.sleep(30)
    if mode == "crash":
        os._exit(9)
    if mode == "error":
        print(json.dumps({"ok": False, "error": "ValueError"}), flush=True)
        continue
    with open(request["out"], "wb") as out:
        out.write(b"\xff\xd8jpeg")
    print(json.dumps({"ok": True, "seconds": 0.02}), flush=True)
'''


def _drawer(tmp_path: Path, mode: str) -> tuple[Drawer, Path]:
    script = tmp_path / "fake_draw.py"
    script.write_text(FAKE, encoding="utf-8")
    seen = tmp_path / "seen.jsonl"
    env = {"FAKE_MODE": mode, "FAKE_SEEN": str(seen), "PATH": "/usr/bin:/bin"}
    return Drawer([sys.executable, "-I", str(script)], env), seen


def test_draw_round_trip(tmp_path):
    drawer, _ = _drawer(tmp_path, "ok")
    assert drawer.start(timeout=20) == pytest.approx(0.01)
    assert drawer.running
    out = tmp_path / "a.jpg"
    assert drawer.draw("a robot", 1024, 768, 7, out, timeout=20) == pytest.approx(0.02)
    assert out.read_bytes() == b"\xff\xd8jpeg"
    drawer.stop()
    assert not drawer.running


def test_prompt_travels_only_in_json(tmp_path):
    drawer, seen = _drawer(tmp_path, "ok")
    drawer.start(timeout=20)
    nasty = "x\"; rm -rf ~ $(id) `whoami`\n{\"ok\": true}"
    drawer.draw(nasty, 512, 512, 1, tmp_path / "b.jpg", timeout=20)
    drawer.stop()
    record = json.loads(seen.read_text(encoding="utf-8").splitlines()[0])
    assert record["request"]["prompt"] == nasty
    assert record["request"]["width"] == 512 and record["request"]["seed"] == 1
    assert record["request"]["quality"] == 90
    assert all(nasty not in part for part in record["argv"])


def test_draw_timeout_kills_process(tmp_path):
    drawer, _ = _drawer(tmp_path, "slow")
    drawer.start(timeout=20)
    started = time.monotonic()
    with pytest.raises(DrawerTimeout):
        drawer.draw("p", 64, 64, 1, tmp_path / "c.jpg", timeout=1)
    assert time.monotonic() - started < 15
    assert not drawer.running


def test_drawer_crash_mid_request_raises(tmp_path):
    drawer, _ = _drawer(tmp_path, "crash")
    drawer.start(timeout=20)
    with pytest.raises(DrawerError) as error:
        drawer.draw("p", 64, 64, 1, tmp_path / "d.jpg", timeout=20)
    assert not isinstance(error.value, DrawerTimeout)
    assert not drawer.running


def test_error_reply_raises(tmp_path):
    drawer, _ = _drawer(tmp_path, "error")
    drawer.start(timeout=20)
    with pytest.raises(DrawerError):
        drawer.draw("p", 64, 64, 1, tmp_path / "e.jpg", timeout=20)
    drawer.stop()


def test_draw_without_start_raises(tmp_path):
    drawer, _ = _drawer(tmp_path, "ok")
    with pytest.raises(DrawerError):
        drawer.draw("p", 64, 64, 1, tmp_path / "f.jpg", timeout=5)


def test_stop_is_idempotent(tmp_path):
    drawer, _ = _drawer(tmp_path, "ok")
    drawer.stop()
    drawer.start(timeout=20)
    drawer.stop()
    drawer.stop()
    assert not drawer.running


def test_idle_seconds_counts_from_last_use(tmp_path):
    drawer, _ = _drawer(tmp_path, "ok")
    drawer.start(timeout=20)
    used = time.monotonic()
    assert drawer.idle_seconds(used + 100) == pytest.approx(100, abs=2)
    drawer.stop()


def test_drawer_argv_and_env(isolated_omm_home):
    venv, snapshot = Path("/v"), Path("/snap")
    argv = poster_drawer.drawer_argv(venv, snapshot)
    assert argv[:3] == ["/v/bin/python", "-I", str(Path(poster_drawer.__file__).with_name("poster_draw.py"))]
    assert argv[3:] == ["--model-path", "/snap", "--model", "flux2-klein-4b", "--quantize", "8", "--steps", "4"]
    env = poster_drawer.drawer_env()
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HF_HOME"].endswith("cluster/poster/hf")
