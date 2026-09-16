import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_cf_worker_lock_drops_unused_worker_pool_and_resolves_patched_sharp():
    package_path = ROOT / "cf-worker" / "package.json"
    lock_path = ROOT / "cf-worker" / "package-lock.json"
    if not package_path.is_file() or not lock_path.is_file():
        pytest.skip("cf-worker is excluded from the runtime Docker image")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert "@cloudflare/vitest-pool-workers" not in package["devDependencies"]
    assert not any(
        path == "node_modules/@cloudflare/vitest-pool-workers"
        or path.startswith("node_modules/@cloudflare/vitest-pool-workers/")
        for path in lock["packages"]
    )
    resolved = {
        record["version"]
        for path, record in lock["packages"].items()
        if path == "node_modules/sharp" or path.endswith("/node_modules/sharp")
    }
    assert resolved == {"0.35.4"}
