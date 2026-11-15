"""Read the current completed level's net score, never HUD or personal bests.

Result anchors must identify the completed, settled results screen (including
its final continuation prompt), rather than the end-of-timer animation.
All rectangles and patterns are supplied by human-recording calibration.
"""
import re

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer, _box_center_in
from coding4game_gui.benchmark.windows.scorers.overcooked_contract import validate_overcooked_spec


class Overcooked2Scorer(OcrScorer):
    def __init__(self, spec, client_size, ocr=None):
        validate_overcooked_spec(spec)
        super().__init__(spec, client_size=client_size, ocr=ocr)

    def reset(self):
        super().reset()
        self._cache_key = self._cache_val = None

    def _read(self, frame):
        # Tiny digit changes must not collide with the shared sparse cache key.
        key = (frame.shape, frame.dtype.str, frame.tobytes())
        if key != self._cache_key:
            self._cache_val = self.ocr(frame)
            self._cache_key = key
        return self._cache_val

    def _match(self, items, rule):
        rect = self.scale_rect(rule['rect'])
        local = sorted((it for it in items if _box_center_in(it['box'], rect)),
                       key=lambda it: (it['box'][1], it['box'][0]))
        text = ' '.join(it['text'].strip() for it in local)
        return re.fullmatch(rule['pattern'], text, re.I)

    def _anchors(self, items, group):
        return all(self._match(items, rule) for rule in self.spec[group]['anchors'])

    def snapshot(self, frame):
        analysis = self.inspect_results(frame)
        if not analysis['complete']:
            return None
        net = analysis['values']['net_score']
        threshold = self.spec['three_star_threshold']
        completed = self._match(analysis['items'], self.spec['result']['anchors'][1])
        count = re.search(r'[xX×]\s*(\d+)\s*$', completed.group(0))
        return dict(score=min(100.0, 100.0 * net / threshold),
                    net_score=net, three_star_threshold=threshold,
                    success=net >= threshold, phase='results',
                    recognition_version='overcooked_results_v2',
                    completed_orders=int(count.group(1)) if count else None)

    def inspect_results(self, frame):
        """Per-capture facts only; temporal confirmation belongs to the host."""
        items = self._read(frame)
        conditions = {}
        rules = self.spec['result']

        def check(name, rule, readings):
            local = [it for it in readings
                     if _box_center_in(it['box'], self.scale_rect(rule['rect']))]
            match = self._match(readings, rule)
            conditions[name] = dict(passed=match is not None,
                                    reason='matched' if match else 'missing_or_ambiguous',
                                    rect=list(self.scale_rect(rule['rect'])), items=local)
            return match

        anchors = [bool(check(f'anchor_{i}', rule, items))
                   for i, rule in enumerate(rules['anchors'])]
        candidate = anchors[0] and any(anchors[1:])
        values = {}
        targeted = None
        numeric_ocr = {}
        for name in ('completed_amount', 'tips_amount', 'failed_amount', 'net_score'):
            rule = rules[name]
            match = check(name, rule, items)
            # A missing total is a measured full-screen OCR failure. Ambiguous
            # or conflicting full-screen digits must never be replaced by a crop.
            if (all(anchors) and match is None
                    and not conditions[name]['items']):
                x, y, w, h = map(int, self.scale_rect(rule['rect']))
                raw = self.ocr(frame[y:y+h, x:x+w])
                mapped = [dict(it, box=[it['box'][0]+x, it['box'][1]+y,
                                       it['box'][2], it['box'][3]]) for it in raw]
                crop_record = dict(rect=[x, y, w, h], raw_items=raw, mapped_items=mapped,
                                coordinate_mapping='crop pixels + native crop origin')
                numeric_ocr[name] = crop_record
                if name == 'net_score':
                    targeted = crop_record
                original = conditions[name]
                match = check(name, rule, mapped)
                conditions[name]['full_frame'] = original
            value = None
            if match and re.fullmatch(r'-?\d+', match.group(1), re.ASCII):
                value = int(match.group(1))
            values[name] = value
            conditions[name]['passed'] = value is not None
        readable = all(value is not None for value in values.values())
        arithmetic = (readable and values['completed_amount'] >= 0
                      and values['tips_amount'] >= 0 and values['failed_amount'] <= 0
                      and values['completed_amount'] + values['tips_amount']
                      + values['failed_amount'] == values['net_score'])
        conditions['accounting'] = dict(passed=arithmetic,
            reason='consistent' if arithmetic else 'unreadable_or_inconsistent',
            equation='completed_amount + tips_amount + failed_amount == net_score')
        complete = all(anchors) and readable and arithmetic
        return dict(recognition_version='overcooked_results_v2', items=items,
                    targeted_ocr=targeted, numeric_ocr=numeric_ocr, conditions=conditions, values=values,
                    candidate=candidate, complete=complete,
                    diagnostic=('complete' if complete else
                                'complete_structure_values_unreadable' if all(anchors) and not readable else
                                'candidate_incomplete' if candidate else 'no_candidate_observed'))

    def check_start(self, frame):
        items = self._read(frame)
        return self._anchors(items, 'ready') and not self._anchors(items, 'result')

    def is_terminal(self, frame):
        return self.snapshot(frame) is not None

    def classify_phase(self, frame):
        result = self.snapshot(frame)
        if result is not None:
            return 'success' if result['success'] else 'failure'
        return 'ready' if self.check_start(frame) else None

    def delta(self, frame):
        raise RuntimeError('overcooked_2 scores completed snapshots, not frame deltas')
