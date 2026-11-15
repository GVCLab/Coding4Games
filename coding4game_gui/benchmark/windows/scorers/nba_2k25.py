"""Settle the historical Blacktop point contract from the labeled score HUD."""
import re

from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer, ScorerLost


class Nba2k25Scorer(OcrScorer):
    def reset(self):
        super().reset()
        self._nba_frame_key = None
        self._nba_items = None

    def _read(self, frame):
        # A one-pixel digit stroke must invalidate the sparse generic cache.
        key = (frame.shape, frame.dtype.str, frame.tobytes())
        if key != self._nba_frame_key:
            recognize = getattr(self.ocr, 'recognize', None)
            labels = self.spec.get('hud_labels')
            if callable(recognize) and labels:
                items = []
                regions = [(rule, False) for rule in labels.values()]
                regions += [(rule, True) for rule in self.spec['regions'].values()]
                for rule, numeric in regions:
                    x, y, width, height = map(lambda n: int(round(n)), self.scale_rect(rule['rect']))
                    crop = frame[y:y + height, x:x + width]
                    for item in recognize(crop, trim_light_text=True):
                        text = item['text'].strip()
                        if numeric:
                            text = ''.join(text.split())
                            if not re.fullmatch(r'[0-9]{2}', text):
                                continue
                        bx, by, bw, bh = item['box']
                        guard = self.spec.get('hud_digit_guard') if numeric else None
                        if guard:
                            ix, iy = guard['inset']
                            ix *= self.rect_scale[0]
                            iy *= self.rect_scale[1]
                            if (bx < ix or by < iy or bx + bw > width - ix
                                    or by + bh > height - iy
                                    or bh > guard['max_height'] * self.rect_scale[1]):
                                continue
                        items.append({**item, 'text': text,
                                      'box': [x + bx, y + by, bw, bh]})
            else:
                self._cache_key = None
                items = super()._read(frame)
            self._nba_items = items
            self._nba_frame_key = key
        return self._nba_items

    def snapshot(self, frame):
        reading = super().snapshot(frame)
        if reading is None:
            return None
        points = reading['readings']
        if set(points) != {'points_for', 'points_against'}:
            raise ValueError('NBA scoring requires both labeled point counters')
        if not all(0 <= value <= 99 for value in points.values()):
            return None
        return reading

    def begin_episode(self, frame):
        reading = self.snapshot(frame)
        if reading is None or any(reading['readings'].values()):
            raise ScorerLost('NBA requires a readable initial 0-0 score HUD')
