"""The vote wire contract lives in three places that must agree: the Python
payload builder (arena_upload.WIRE_FIELDS), the Worker validator
(cf-worker/src/validate.ts), and the documented RTDB rules
(database.rules.json). The memory-group labels add a fourth: memory_group."""

from __future__ import annotations

import json
import re
from pathlib import Path

from omm import arena_upload, memory_group

ROOT = Path(__file__).resolve().parents[1]


def _vote_rules() -> dict:
    rules = json.loads((ROOT / "database.rules.json").read_text(encoding="utf-8"))
    return rules["rules"]["votes"]["$vote"]


def _validate_ts() -> str:
    return (ROOT / "cf-worker/src/validate.ts").read_text(encoding="utf-8")


def test_rules_keys_equal_the_python_wire_fields():
    keys = set(_vote_rules()) - {".validate", "$other"}
    assert keys == arena_upload.WIRE_FIELDS


def test_validate_ts_allows_the_memory_group_fields():
    block = re.search(r"const VOTE_FIELDS = new Set<string>\(\[(.*?)\]\);", _validate_ts(), re.S)
    assert block is not None
    assert '"memory_kind"' in block.group(1)
    assert '"memory_bucket"' in block.group(1)


def test_validate_ts_declares_every_bucket_label():
    block = re.search(r"VOTE_MEMORY_BUCKETS[^{]*\{(.*?)\};", _validate_ts(), re.S)
    assert block is not None
    for kind, labels in memory_group.BUCKET_LABELS.items():
        found = re.search(rf"{kind}:\s*new Set\(\[(.*?)\]\)", block.group(1), re.S)
        assert found is not None, kind
        assert sorted(re.findall(r'"([^"]+)"', found.group(1))) == sorted(labels)


def test_rules_declare_every_bucket_label_under_its_kind():
    rule = _vote_rules()["memory_bucket"][".validate"]
    for kind, labels in memory_group.BUCKET_LABELS.items():
        clause = re.search(
            rf"child\('memory_kind'\)\.val\(\) == '{kind}' && \((.*?)\)\)", rule
        )
        assert clause is not None, kind
        assert sorted(re.findall(r"newData\.val\(\) == '([^']+)'", clause.group(1))) == sorted(
            labels
        )


def test_rules_require_the_pair_together():
    assert (
        "newData.child('memory_kind').exists() == newData.child('memory_bucket').exists()"
        in _vote_rules()[".validate"]
    )
    kind_rule = _vote_rules()["memory_kind"][".validate"]
    for kind in memory_group.KINDS:
        assert f"'{kind}'" in kind_rule
