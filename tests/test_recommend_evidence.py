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


def _title(row, width):
    from omm import recommend_ui
    return "".join(text for _, text in recommend_ui.choice_title(row, width))


def test_list_rows_name_the_evidence_level_without_accuracy_numbers():
    from omm import recommend_ui
    hw,candidate,row=inputs()
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[row],[])}
    synthetic={**artifact,'real_row_count':0,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[],[])}
    other={**candidate,'filename':'model-8B-Q4_K_M.gguf'}
    cases=[(artifact,candidate,'measured'),(artifact,other,'unverified'),(None,candidate,'rules'),(synthetic,candidate,'synthetic')]
    for art,cand,marker in cases:
        [(annotated,_)]=evidence.annotate([(cand,20.0)],art,hw)
        assert recommend_ui.evidence_marker(annotated)==marker
        [ui_row]=recommend_ui.build_rows([(annotated,20.0)],['x'])
        title=_title(ui_row,120)
        assert marker in title and '%' not in title
        assert recommend_ui.choice_header(120).plain.index('BASIS')-3==title.index(marker)
        assert len(title)<=120-4
    assert recommend_ui.evidence_marker(candidate)==''


def test_evidence_annotation_keeps_ranking_and_install_targets():
    from omm import recommend_ui
    hw,candidate,row=inputs()
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[row],[])}
    ranked=[({'repo_id':'org/model-8B','filename':'model-8B-Q4_K_M.gguf'},40.0),(candidate,30.0),
            ({'repo_id':'org/model-3B','filename':'model-3B-Q4_K_M.gguf'},None)]
    refs=['org/model-8B:model-8B-Q4_K_M.gguf','org/model-1B:model-1B-Q4_K_M.gguf','org/model-3B:model-3B-Q4_K_M.gguf']
    for art in (artifact,None):
        annotated=evidence.annotate(ranked,art,hw)
        assert [(c['repo_id'],c['filename'],s) for c,s in annotated]==[(c['repo_id'],c['filename'],s) for c,s in ranked]
        plain=recommend_ui.build_rows(ranked,refs)
        marked=recommend_ui.build_rows(annotated,refs)
        assert [r.value for r in marked]==[r.value for r in plain]==refs
        assert [r.speed for r in marked]==[r.speed for r in plain]
        assert [r.badge for r in marked]==[r.badge for r in plain]
        assert all('recommendation_evidence' not in c for c,_ in ranked)


def test_basis_classes_and_counts_stay_separate_without_accuracy_claims():
    hw,candidate,row=inputs()
    support=evidence.build_support(predictor.FEATURE_ORDER,[row,row,row],[])
    artifact={'feature_order':predictor.FEATURE_ORDER,'measurement_support':support,
              'raw_real_row_count':30,'real_row_count':12,
              'telemetry_audit':{'repetition_balance':{'repeat_samples_collapsed':18,'independent_device_count':None}}}
    other={**candidate,'filename':'model-8B-Q4_K_M.gguf'}
    measured=evidence.describe(artifact,hw,candidate)
    assert measured['basis']=='similar_measured'
    assert measured['matching_feature_configurations']=={'training':1,'holdout':0}  # 3 repeats, 1 configuration
    assert measured['catalog_measurements']=={'raw_rows':30,'valid_rows':None,'repeat_samples_collapsed':18,
                                              'distinct_configurations':12,'independent_devices':None}
    assert evidence.describe(artifact,hw,other)['basis']=='insufficient_evidence'
    assert evidence.describe(artifact,hw,candidate,engine='lmstudio')['basis']=='insufficient_evidence'
    assert evidence.describe(None,hw,candidate)['basis']=='rules_or_synthetic'
    assert evidence.describe(None,hw,candidate)['catalog_measurements'] is None
    assert evidence.describe({**artifact,'real_row_count':0},hw,candidate)['basis']=='rules_or_synthetic'
    for result in (measured,evidence.describe(None,hw,candidate)):
        assert result['calibrated_interval'] is False and result['independent_device_count'] is None


def test_old_catalog_without_evidence_fields_loads_with_unknown_counts():
    hw,candidate,_=inputs()
    result=evidence.describe({'feature_order':predictor.FEATURE_ORDER,'real_row_count':'bad','telemetry_audit':[]},hw,candidate)
    assert result['basis']=='insufficient_evidence'
    assert result['catalog_measurements']=={'raw_rows':None,'valid_rows':None,'repeat_samples_collapsed':None,
                                            'distinct_configurations':None,'independent_devices':None}


def test_local_calibration_counts_runs_on_one_device(monkeypatch):
    from omm import calibration
    hw,candidate,_=inputs()
    key=calibration.hardware_bucket(hw)
    monkeypatch.setattr(calibration,'load_profiles',lambda: {'profiles':{key:{'factor':0.9,'sample_count':5}}})
    result=evidence.describe({'feature_order':predictor.FEATURE_ORDER},hw,candidate)
    assert result['basis']=='measurement_estimate'
    assert result['local_calibration_samples']==5 and result['local_calibration_devices']==1


def test_detail_lines_separate_counts_and_never_show_accuracy():
    from omm import recommend_ui
    hw,candidate,row=inputs()
    artifact={'feature_order':predictor.FEATURE_ORDER,'raw_real_row_count':30,'real_row_count':12,
              'measurement_support':evidence.build_support(predictor.FEATURE_ORDER,[row],[row])}
    lines=recommend_ui.evidence_detail_lines(evidence.describe(artifact,hw,candidate))
    text='\n'.join(lines)
    assert 'similar hardware' in text
    assert 'training 1, holdout 1' in text
    assert '30 raw rows (unknown valid), unknown repeated runs merged, 12 distinct configurations' in text
    assert 'No calibrated accuracy' in text and '%' not in text
    old=recommend_ui.evidence_detail_lines({'status':'insufficient'})
    assert old[0].startswith('Prediction evidence: insufficient evidence')


def test_published_catalog_reports_raw_valid_repeat_and_configuration_counts():
    import json
    from pathlib import Path
    hw,candidate,_=inputs()
    artifact=json.loads((Path(__file__).resolve().parents[1]/'published'/'localfit-recommend-model.json').read_text(encoding='utf-8'))
    audit=artifact['telemetry_audit']
    counts=evidence.describe(artifact,hw,candidate)['catalog_measurements']
    assert counts['raw_rows']==artifact['raw_real_row_count']
    assert counts['valid_rows']==audit['valid_rows']
    assert counts['distinct_configurations']==artifact['real_row_count']
    assert counts['repeat_samples_collapsed'] is not None and counts['independent_devices'] is None
