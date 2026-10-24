"""Exercise macOS managed installation, or prove an unhashed cask is blocked.

A blocked result verifies refusal only; it is never an install/link test pass.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omm import install_safety  # noqa: E402
from omm.engine_packages import PACKAGES  # noqa: E402


def verify(engine):
    cask = PACKAGES[engine].brew_cask
    data = install_safety._json(["brew", "info", "--json=v2", "--cask", "homebrew/cask/" + cask])
    rows = data.get("casks", [])
    if (len(rows) == 1 and rows[0].get("token") == cask and
            rows[0].get("tap") == "homebrew/cask" and rows[0].get("sha256") == "no_check"):
        try:
            install_safety.prepare(engine, ["brew", "install", "--cask", cask])
        except install_safety.SafetyError as error:
            if "Official fixed-version cask with HTTPS and SHA-256 is required" not in str(error):
                raise
            print(f"BLOCKED: {engine} official cask has no pinned SHA-256. "
                  "Mandatory refusal verified; installation/link/import NOT VERIFIED.")
            return "blocked"
        raise RuntimeError("Mandatory preflight accepted an unhashed cask")
    subprocess.run([sys.executable, str(Path(__file__).with_name("verify_engine_link.py")), engine], check=True)
    return "installed_and_linked"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine", choices=["anythingllm", "mstystudio"])
    verify(parser.parse_args().engine)
