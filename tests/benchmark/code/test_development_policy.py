import hashlib
import json
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.code.development_policy import (
    load_budget_notice, review_fraction, review_notice,
)


@pytest.mark.parametrize('value', [True, 0, 1.1, float('nan'), '0.5'])
def test_review_fraction_rejects_ambiguous_or_invalid_policy(value):
    with pytest.raises(ValueError, match='fraction'):
        review_fraction(value)


def test_default_task_is_unchanged_and_thresholds_round_up_without_granting_resources():
    assert review_notice(None, tic_budget=10000, token_limit=20000000) == ''
    notice = review_notice(.5, tic_budget=10001, token_limit=20000001)
    assert '5001 tics' in notice and '10000001 tokens' in notice
    assert 'until either' in notice and ' or cumulative development' in notice
    assert 'not required' in notice and 'original budget ceilings' in notice
    assert 'confirmed subtotal' in notice and 'pointless waits' in notice
    assert 'No independent final-test feedback' in notice


def test_unlimited_tokens_do_not_invent_a_model_token_allowance():
    notice = review_notice(.5, tic_budget=10000, token_limit=None)
    assert '5000 tics' in notice and 'confirmed model-token usage reaches' not in notice


@pytest.fixture
def notice(tmp_path):
    root = tmp_path / 'run'; root.mkdir()
    raw = b'{"status":"failed","token_limit":20000000,"tic_budget":10000}'
    grant = {'kind': 'c4g-recovery-budget-notice-v1', 'run_dir': str(root),
             'source_config_sha256': hashlib.sha256(raw).hexdigest(),
             'minimum_development_fraction': .5, 'authorization_basis': 'Human approved50% review.'}
    path = tmp_path / 'host-notice.json'; path.write_text(json.dumps(grant))
    return SimpleNamespace(recovery_budget_notice=str(path)), root, raw, path, grant


def test_notice_is_read_only_bound_to_configuration_and_contains_no_free_form_prompt(notice):
    args, root, raw, path, grant = notice
    before = path.read_bytes()
    result = load_budget_notice(args, root, raw)
    assert result['authorization'] == grant and path.read_bytes() == before
    assert result['authorization_sha256'] == hashlib.sha256(before).hexdigest()
    assert not list(root.iterdir())
    with pytest.raises(ValueError, match='configuration'):
        load_budget_notice(args, root, raw + b' ')


@pytest.mark.parametrize('field,value', [('run_dir', '/other'), ('source_config_sha256', '0'*64),
    ('authorization_basis', ''), ('kind', 'wrong'), ('prompt', 'unapproved strategy')])
def test_notice_cannot_smuggle_a_task_or_match_another_run(notice, field, value):
    args, root, raw, path, grant = notice
    grant[field] = value; path.write_text(json.dumps(grant))
    with pytest.raises(ValueError, match='notice'):
        load_budget_notice(args, root, raw)


@pytest.mark.parametrize('payload', [b'[]', b'\xff', b'{', b'x'*16385])
def test_malformed_notice_is_refused_before_mutation(notice, payload):
    args, root, raw, path, _ = notice; path.write_bytes(payload)
    with pytest.raises(ValueError, match='notice'):
        load_budget_notice(args, root, raw)
    assert not list(root.iterdir())
