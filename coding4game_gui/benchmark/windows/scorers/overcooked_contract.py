"""Dependency-free validation of Overcooked's measured settlement contract."""
import math
import re


def validate_overcooked_spec(spec):
    """Reject missing measurements before a game or paid player is started."""
    if spec.get('score_version') != 'overcooked_2_2_1_composite_v1':
        raise ValueError('Overcooked requires score_version overcooked_2_2_1_composite_v1')
    if spec.get('recognition_version') != 'overcooked_results_v2':
        raise ValueError('Overcooked requires recognition_version overcooked_results_v2')
    if spec.get('settlement') != 'latest_valid_frame':
        raise ValueError('overcooked_2 requires latest_valid_frame settlement')
    threshold = spec.get('three_star_threshold')
    if type(threshold) is not int or threshold <= 0:
        raise ValueError('overcooked_2 requires a measured positive three_star_threshold')
    size = spec.get('calibrated_client_size')
    if (not isinstance(size, (list, tuple)) or len(size) != 2
            or any(type(v) is not int or v <= 0 for v in size)):
        raise ValueError('overcooked_2 requires calibrated_client_size')
    for group in ('result', 'ready'):
        rules = spec.get(group) or {}
        if not rules.get('anchors'):
            raise ValueError(f'overcooked_2 requires measured {group}.anchors')
        entries = list(rules['anchors'])
        if group == 'result':
            entries.append(rules.get('net_score') or {})
            if len(rules['anchors']) != 6:
                raise ValueError('overcooked_results_v2 requires all six calibrated anchors')
            for name in ('completed_amount', 'tips_amount', 'failed_amount'):
                rule = rules.get(name) or {}
                if re.compile(rule.get('pattern', '')).groups != 1:
                    raise ValueError(f'overcooked_results_v2 requires {name} with one capture')
                entries.append(rule)
        for rule in entries:
            rect = rule.get('rect')
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in rect)
                    or min(rect[:2]) < 0 or min(rect[2:]) <= 0
                    or rect[0] + rect[2] > size[0] or rect[1] + rect[3] > size[1]):
                raise ValueError(f'overcooked_2 requires a measured {group} rectangle')
            if not isinstance(rule.get('pattern'), str) or not rule['pattern']:
                raise ValueError(f'overcooked_2 requires a measured {group} pattern')
            re.compile(rule['pattern'])
    if re.compile(spec['result']['net_score']['pattern']).groups != 1:
        raise ValueError('overcooked_2 net_score pattern requires exactly one capture group')
