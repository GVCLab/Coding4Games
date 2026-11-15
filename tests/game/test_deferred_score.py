import numpy as np
from coding4game_gui.benchmark.windows.deferred_score import DeferredScore


def test_final_frame_does_not_promote_initial_zero_when_final_hud_is_missing():
    class Scorer:
        spec = {'settlement': 'final_frame'}
        def reset(self): pass
        def delta(self, _frame): return 0
        def snapshot(self, frame):
            return {'score': 0} if not frame[0,0,0] else None
    pending = DeferredScore()
    pending.add(np.zeros((8,8,3),np.uint8), tics=0)
    pending.add(np.ones((8,8,3),np.uint8), tics=1000)
    assert pending.settle(Scorer()) is None
    assert pending.details['status'] == 'no_valid_frame'
    assert pending.selected_png is None
    assert pending.endpoint_png is not None
    assert pending.details['endpoint_frame_tic'] == 1000
    import cv2
    assert np.all(cv2.imdecode(np.frombuffer(pending.endpoint_png, np.uint8), cv2.IMREAD_COLOR) == 1)


def test_final_frame_settles_only_the_actual_last_frozen_observation():
    class Scorer:
        spec = {'settlement': 'final_frame'}
        def reset(self): pass
        def delta(self, _frame): return 0
        def snapshot(self, frame): return {'score': -5 * int(frame[0,0,0])}
    pending = DeferredScore()
    pending.add(np.zeros((8,8,3),np.uint8), tics=0)
    pending.add(np.full((8,8,3),7,np.uint8), tics=1000)
    assert pending.settle(Scorer()) == -35
    assert pending.details['selected_frame_tic'] == 1000


def test_region_recognition_survives_production_deferred_wrapper_and_settlement():
    from coding4game_gui.benchmark.spec import load_game_spec
    from coding4game_gui.benchmark.windows.scorers.kingdom_rush_alliance import KingdomRushAllianceScorer

    class Ocr:
        def __call__(self, frame):
            text = '0/10' if frame[0, 0, 0] == 1 else '胜利'
            return [dict(text=text, conf=.99, box=[60, 76, 70, 30])]

        def recognize(self, crop, *, trim_light_text):
            assert crop.shape == (35, 50, 3) and trim_light_text
            return [dict(text='20', conf=.99, box=[2, 2, 40, 25])]

    engine = Ocr()
    spec = load_game_spec('kingdom_rush_alliance_heart_of_the_forest')['scorer']
    scorer = KingdomRushAllianceScorer(spec, (2560, 1440), ocr=engine)
    pending = DeferredScore(scorer)
    for tag, terminal in [(1, False), (2, True)]:
        frame = np.full((1440, 2560, 3), tag, np.uint8)
        pending.add(frame)
        assert scorer.is_terminal(frame) is terminal
        assert pending.value is None and scorer.total == 0
    assert pending.settle(scorer) == 100
    assert pending.details['ocr_reuse'] == {'hits': 3, 'misses': 3}
    assert scorer.ocr is engine
    assert pending.settle(scorer) == 100


def test_ocr_cache_separates_detection_recognition_and_preprocessing():
    from coding4game_gui.benchmark.windows.scorers.perception_cache import EpisodeOcrCache

    class Engine:
        def __call__(self, frame):
            return ['detected']

        def recognize(self, frame, *, trim_light_text):
            return ['trimmed' if trim_light_text else 'whole']

    cache = EpisodeOcrCache(Engine())
    frame = np.zeros((35, 50, 3), np.uint8)
    try:
        for _ in range(2):
            assert cache(frame) == ['detected']
            assert cache.recognize(frame) == ['whole']
            assert cache.recognize(frame, trim_light_text=True) == ['trimmed']
        assert (cache.hits, cache.misses) == (3, 3)
    finally:
        cache.close()


def test_terminal_ocr_is_reused_without_settling_during_play():
    from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
    spec = dict(calibrated_client_size=[64, 32],
                phases={'death': {'any': ['RUN LOST']},
                        'combat': {'pattern': r'WAVE\s*\d'}},
                regions={'wave': {'pattern': r'WAVE\s*(\d+)', 'baseline': 0}},
                weights={'wave': 100})
    calls = []

    def ocr(frame):
        calls.append(int(frame[0, 0, 0]))
        return [dict(text='WAVE 1', conf=.99, box=[0, 0, 10, 10])]

    scorer = OcrScorer(spec, [64, 32], ocr=ocr)
    pending = DeferredScore(scorer)
    for value in range(20):
        frame = np.full((32, 64, 3), value, np.uint8)
        pending.add(frame)
        assert not scorer.is_terminal(frame)
        assert pending.value is None and scorer._total == 0
    assert len(calls) == 20
    assert pending.settle(scorer) == 100
    assert len(calls) == 20
    assert scorer.ocr is ocr
    assert pending.details['ocr_reuse'] == {'hits': 20, 'misses': 20}
    assert pending.settle(scorer) == 100


def test_reuse_preserves_ordered_scoring_and_missing_online_reads():
    from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
    spec = dict(calibrated_client_size=[64, 32],
                phases={'shop': {'any': ['SHOP']}, 'combat': {'pattern': r'WAVE\s*\d'}},
                regions={'wave': {'pattern': r'WAVE\s*(\d+)', 'baseline': 0,
                                  'max_increment': 1}}, weights={'wave': 100})
    text = ['WAVE 1', 'SHOP', 'WAVE 2', 'WAVE 8', 'WAVE 3']
    frames = [np.full((32, 64, 3), tag, np.uint8) for tag in range(len(text))]
    scores = []
    for reuse in [False, True]:
        def engine(frame):
            return [dict(text=text[int(frame[0, 0, 0])], conf=.9, box=[0, 0, 10, 10])]
        scorer = OcrScorer(spec, [64, 32], ocr=engine)
        pending = DeferredScore(scorer if reuse else None)
        for i, frame in enumerate(frames):
            pending.add(frame)
            if i % 2 == 0:
                scorer.is_terminal(frame)
        scores.append(pending.settle(scorer))
        assert scorer.ocr is engine
        if reuse:
            assert pending.details['ocr_reuse'] == {'hits': 3, 'misses': 5}
    assert scores == [300, 300]


def test_reuse_restores_engine_and_closes_files_on_settlement_error():
    import pytest
    from pathlib import Path
    from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer, ScorerLost
    engine = lambda frame: []
    scorer = OcrScorer(dict(phases={}, regions={}, miss_limit=1), [64, 32], ocr=engine)
    pending = DeferredScore(scorer)
    directory = Path(pending._ocr_cache._directory.name)
    frame = np.zeros((32, 64, 3), np.uint8)
    pending.add(frame)
    scorer.is_terminal(frame)
    with pytest.raises(ScorerLost):
        pending.settle(scorer)
    assert scorer.ocr is engine and not directory.exists() and pending.stream.closed
    pending.close()


def test_deferred_lossless_order_and_single_settlement():
    class Scorer:
        calls = 0
        resets = 0
        def reset(self): self.resets += 1
        def delta(self, frame):
            self.calls += 1
            return int(frame[0, 0, 0])
    scorer = Scorer()
    pending = DeferredScore()
    for value in [3, 5, 2]:
        pending.add(np.full((5, 5, 3), value, np.uint8))
    assert scorer.calls == 0
    assert pending.settle(scorer) == 10
    assert pending.settle(scorer) == 10
    assert scorer.calls == 3 and scorer.resets == 1


def test_scoring_failure_is_not_a_zero_and_is_not_retried():
    import pytest
    class Scorer:
        calls = 0
        def reset(self): pass
        def delta(self, frame):
            self.calls += 1
            raise ValueError('bad evidence')
    scorer = Scorer()
    pending = DeferredScore()
    pending.add(np.zeros((5, 5, 3), np.uint8))
    with pytest.raises(ValueError): pending.settle(scorer)
    with pytest.raises(RuntimeError): pending.settle(scorer)
    assert pending.value is None and scorer.calls == 1
    assert pending.stream.closed


def test_latest_valid_scoring_skips_unreadable_tail_and_stops():
    class Scorer:
        spec = {'settlement': 'latest_valid_frame'}
        calls = []
        def snapshot(self, frame):
            value = int(frame[0, 0, 0])
            self.calls.append(value)
            return None if value == 9 else {'score': value, 'readings': {'value': value}}
    pending = DeferredScore()
    for value in [2, 5, 9, 9]:
        pending.add(np.full((5, 5, 3), value, np.uint8))
    scorer = Scorer()
    assert pending.settle(scorer) == 5
    assert scorer.calls == [9, 9, 5]
    assert pending.details['selected_frame_index'] == 1
    assert pending.details['skipped_frames'] == 2
    assert pending.settle(scorer) == 5
    assert scorer.calls == [9, 9, 5]


def test_latest_valid_no_readout_is_null_and_settles_once():
    class Scorer:
        spec = {'settlement': 'latest_valid_frame'}
        def snapshot(self, frame): return None
    pending = DeferredScore()
    pending.add(np.zeros((5, 5, 3), np.uint8))
    assert pending.settle(Scorer()) is None
    assert pending.settle(Scorer()) is None
    assert pending.details['status'] == 'no_valid_frame'
    assert pending.stream.closed
