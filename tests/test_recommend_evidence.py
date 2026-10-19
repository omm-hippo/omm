from __future__ import annotations

from omm import recommend_evidence as evidence, predictor
from omm.hardware import HardwareInfo


def inputs():
    hw=HardwareInfo('macOS','', 'Apple M5',24,20,True,'Apple M5',24,20)
    candidate={'repo_id':'org/model-1B','filename':'model-1B-Q4_K_M.gguf'}
    return hw,candidate,predictor.build_prediction_features(hw,candidate)


def test_duplicate_measurements_are_not_independent_configurations():
    hw,candidate,row=inputs()
    support=evidence.build_support(predictor.FEATURE_ORDER,[row,row],[])
    assert support['real_training_configurations']==1
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':support}
    result=evidence.describe(artifact,hw,candidate)
    assert result['matching_feature_configurations']=={'training':1,'holdout':0}
    assert result['status']=='feature_support'
    assert result['exact_checkpoint_samples'] is None
    assert result['independent_device_count'] is None
    assert result['calibrated_interval'] is False


def test_other_runtime_and_model_features_do_not_inherit_support():
    hw,candidate,row=inputs()
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[row],[])}
    assert evidence.describe(artifact,hw,candidate,engine='lmstudio')['matching_feature_configurations']=={'training':0,'holdout':0}
    different={**candidate,'filename':'model-8B-Q4_K_M.gguf'}
    assert evidence.describe(artifact,hw,different)['matching_feature_configurations']=={'training':0,'holdout':0}


def test_old_catalogs_and_static_rules_do_not_invent_support():
    hw,candidate,_=inputs()
    assert evidence.describe({'feature_order':predictor.FEATURE_ORDER},hw,candidate)['matching_feature_configurations'] is None
    assert evidence.describe(None,hw,candidate)['status']=='static_rules'


def test_training_and_holdout_support_are_separate():
    hw,candidate,row=inputs()
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[],[row])}
    assert evidence.describe(artifact,hw,candidate)['matching_feature_configurations']=={'training':0,'holdout':1}


def test_synthetic_prior_is_not_labeled_as_real_measurement():
    hw,candidate,row=inputs()
    artifact={'real_row_count':0,'feature_order':predictor.FEATURE_ORDER,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[],[])}
    assert evidence.describe(artifact,hw,candidate)['status']=='synthetic_prior'
