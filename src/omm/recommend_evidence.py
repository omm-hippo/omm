"""Real feature support is separate from checkpoint identity and confidence."""
from __future__ import annotations

import hashlib
import json
import math

from omm import calibration, predictor


def feature_key(order, values) -> str:
    if len(order) != len(values):
        raise ValueError('Feature dimensions differ')
    rounded = []
    for value in values:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('Feature values must be finite')
        number = round(number, 3)
        rounded.append(0.0 if number == 0 else number)
    return hashlib.sha256(json.dumps([order, rounded], separators=(',', ':')).encode()).hexdigest()


def build_support(order, training, holdout) -> dict:
    train = {feature_key(order, row) for row in training}
    test = {feature_key(order, row) for row in holdout}
    return {'schema_version': 1, 'feature_order': list(order),
            'real_training_configurations': len(train), 'holdout_configurations': len(test),
            'configurations': {key: {'training': int(key in train), 'holdout': int(key in test)}
                               for key in sorted(train | test)},
            'identity': 'rounded_feature_configuration', 'independent_devices': None,
            'exact_checkpoints': None, 'calibrated_interval': False}


def _count(value):
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


def describe(artifact, hardware, candidate=None, *, engine='ollama') -> dict:
    profile = {}
    if hardware is not None:
        bucket = calibration.hardware_bucket(hardware)
        key = bucket if engine == 'ollama' else bucket+'|'+engine
        profile = calibration.load_profiles().get('profiles', {}).get(key, {})
    if not isinstance(profile, dict):
        profile = {}
    factor = profile.get('factor')
    valid_factor = type(factor) in (int, float) and math.isfinite(factor) and factor > 0
    samples = (_count(profile.get('sample_count')) or 0) if valid_factor else 0
    support = None
    if artifact and hardware is not None and candidate is not None:
        data = artifact.get('measurement_support')
        if isinstance(data, dict) and data.get('schema_version') == 1 and data.get('feature_order') == artifact.get('feature_order'):
            configurations = data.get('configurations')
            if isinstance(configurations, dict):
                key = feature_key(data['feature_order'], predictor.build_prediction_features(hardware, candidate, engine=engine))
                row = configurations.get(key, {'training': 0, 'holdout': 0})
                if isinstance(row, dict) and _count(row.get('training')) is not None and _count(row.get('holdout')) is not None:
                    support = {'training': row['training'], 'holdout': row['holdout']}
    status = 'static_rules' if not artifact else 'local_calibrated' if samples else 'insufficient'
    reason = '기본 메모리 규칙으로 골랐어요. 속도 실측 자료는 없어요.' if not artifact else (
        '로컬 측정으로 속도를 보정했지만, 정확한 모델·장비별 표본 수와 신뢰구간은 제공되지 않아요.' if samples else
        '정확한 모델·장비별 실측 표본 수가 제공되지 않아 신뢰도를 계산할 수 없어요.')
    if artifact and _count(artifact.get('real_row_count')) == 0 and not samples:
        status = 'synthetic_prior'
        reason = '합성 데이터로 만든 예측이에요. 실제 장비에서 측정한 자료는 없어요.'
    if support is not None and not samples and status != 'synthetic_prior':
        status = 'feature_support' if sum(support.values()) else 'insufficient'
        reason = '같은 예측 특성의 실측 구성 수예요. 정확한 체크포인트 일치나 독립 장비 수를 뜻하지 않아요.'
    return {'status': status, 'reason': reason, 'engine': engine,
            'local_calibration_samples': samples, 'matching_environment_samples': None,
            'matching_feature_configurations': support, 'exact_checkpoint_samples': None,
            'independent_device_count': None, 'calibrated_interval': False}


def annotate(ranked, artifact, hardware):
    return [({**candidate, 'recommendation_evidence': describe(artifact, hardware, candidate)}, speed)
            for candidate, speed in ranked]
