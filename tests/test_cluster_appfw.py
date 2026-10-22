"""macOS Application Firewall exceptions for the gateway and exo (#503).

With ALF on, an inbound connection to an unlisted binary is held until
someone clicks Allow on the Mac's screen, so a headless head Mac's gateway
never answers (node3, 2026-10-22). The fixture's first entries are real
`--listapps` output from node3; the "Block" entry is synthesized.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from omm.cluster import appfw
from omm.cluster.errors import ClusterError

FIX = Path(__file__).parent / "fixtures" / "exo"
LISTAPPS = (FIX / "socketfilterfw_listapps.txt").read_text(encoding="utf-8")
SPIKE_EXO = "/Users/shinmingyu/omm-spike/exo-runtime/exo"
BREW_PY = ("/opt/homebrew/Cellar/python@3.15/3.15.0/Frameworks/Python.framework/Versions/3.15"
           "/Resources/Python.app/Contents/MacOS/Python")


class _Fw:
    """`merged` paths are signed copies ALF folds into an existing entry."""

    def __init__(self, listapps=LISTAPPS, enabled=True, sudo_rc=0, merged=()):
        self.listapps, self.enabled, self.sudo_rc, self.calls = listapps, enabled, sudo_rc, []
        self.merged = set(merged)

    def __call__(self, argv, **_k):
        self.calls.append(argv)
        if argv[0] == "sudo":
            if self.sudo_rc == 0 and argv[2] == "--add" and argv[3] not in self.merged:
                self.listapps += f"99 : {argv[3]} \n             (Allow incoming connections)\n"
            return subprocess.CompletedProcess(argv, self.sudo_rc, "", "")
        if argv[1:] == ["--listapps"]:
            return subprocess.CompletedProcess(argv, 0, self.listapps, "")
        if argv[1:] == ["--getglobalstate"]:
            state = "Firewall is enabled. (State = 1)" if self.enabled else "Firewall is disabled. (State = 0)"
            return subprocess.CompletedProcess(argv, 0, state + "\n", "")
        raise AssertionError(argv)

    def sudo_calls(self):
        return [c[1:] for c in self.calls if c[0] == "sudo"]


def test_parse_listapps_reads_allow_and_block():
    assert appfw.parse_listapps(LISTAPPS) == {
        "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app": True,
        SPIKE_EXO: True,
        BREW_PY: False,
    }


def test_allow_adds_and_unblocks_only_what_is_not_allowed(isolated_omm_home):
    fw = _Fw()
    appfw.allow([Path(SPIKE_EXO), Path(BREW_PY), Path("/new/exo")], run=fw)
    assert fw.sudo_calls() == [
        [appfw.SOCKETFILTERFW, "--add", BREW_PY], [appfw.SOCKETFILTERFW, "--unblockapp", BREW_PY],
        [appfw.SOCKETFILTERFW, "--add", "/new/exo"], [appfw.SOCKETFILTERFW, "--unblockapp", "/new/exo"],
    ]


def test_remove_only_touches_entries_omm_added(isolated_omm_home):
    appfw.allow([Path(SPIKE_EXO), Path("/new/exo"), Path("/staged/exo")], run=_Fw(merged={"/staged/exo"}))
    fw = _Fw()
    appfw.remove(run=fw)
    assert fw.sudo_calls() == [[appfw.SOCKETFILTERFW, "--remove", "/new/exo"]]
    assert not appfw.record_path().exists()


def test_rerun_keeps_the_record_of_earlier_additions(isolated_omm_home):
    appfw.allow([Path("/new/exo")], run=_Fw())
    already = LISTAPPS + "9 : /new/exo \n             (Allow incoming connections)\n"
    appfw.allow([Path("/new/exo")], run=_Fw(listapps=already))
    fw = _Fw()
    appfw.remove(run=fw)
    assert fw.sudo_calls() == [[appfw.SOCKETFILTERFW, "--remove", "/new/exo"]]


def test_allow_raises_when_sudo_fails(isolated_omm_home):
    with pytest.raises(ClusterError, match="firewall"):
        appfw.allow([Path("/new/exo")], run=_Fw(sudo_rc=1))


def test_not_allowed_lists_unlisted_and_blocked_while_enabled():
    fw = _Fw()
    assert appfw.not_allowed([Path(SPIKE_EXO), Path(BREW_PY), Path("/new/exo")], run=fw) == [BREW_PY, "/new/exo"]


def test_not_allowed_is_empty_when_the_firewall_is_off():
    assert appfw.not_allowed([Path("/new/exo")], run=_Fw(enabled=False)) == []


def test_gateway_binary_is_the_framework_app_binary_when_present(tmp_path, monkeypatch):
    app = tmp_path / "Resources/Python.app/Contents/MacOS/Python"
    app.parent.mkdir(parents=True)
    app.write_text("", encoding="utf-8")
    monkeypatch.setattr(appfw.sys, "base_prefix", str(tmp_path))
    assert appfw.gateway_binary() == app.resolve()
