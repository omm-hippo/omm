from __future__ import annotations

from omm import mltree
from scripts import model_quality_gate


def test_a_non_bool_leaf_flag_is_traversed_as_a_branch():
    node = {
        "leaf": 1,
        "feature": 0,
        "threshold": 1.0,
        "left": {"leaf": True, "value": 1.0},
        "right": {"leaf": True, "value": 2.0},
    }

    assert mltree.predict_tree(node, [0.5]) == 1.0  # pre-fix: KeyError: 'value'
    assert mltree.predict_tree(node, [5.0]) == 2.0


def test_a_true_leaf_still_returns_its_value():
    assert mltree.predict_tree({"leaf": True, "value": 3.0}, []) == 3.0


def test_is_leaf_matches_the_validators_identity_check():
    for flag in (True, 1, "true", 0, False, None):
        assert mltree.is_leaf({"leaf": flag}) is (flag is True)


def test_a_tree_the_gate_accepts_is_also_evaluable():
    tree = {
        "leaf": 1,
        "feature": 0,
        "threshold": 1.0,
        "left": {"leaf": True, "value": 1.0},
        "right": {"leaf": True, "value": 2.0},
    }
    artifact = {
        "model_version": 1,
        "feature_order": ["ram"],
        "trees": [tree],
    }

    model_quality_gate.validate_artifact(artifact, ["ram"])
    assert mltree.predict_ensemble([tree], [0.5]) == 1.0
