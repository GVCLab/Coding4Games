"""Lossless, disk-backed episode scoring, separate from online lifecycle checks."""
import struct
import tempfile
import time
from dataclasses import asdict, is_dataclass

import cv2
import numpy as np


class DeferredScore:
    def __init__(self, scorer=None, *, retain_replay_evidence=False):
        self.stream = tempfile.TemporaryFile()
        self.value = None
        self.error = None
        self.frame_count = 0
        self.offsets = []
        self.frame_tics = []
        self.settled = False
        self.details = {}
        self.selected_png = None
        self.endpoint_png = None
        self.retain_replay_evidence = retain_replay_evidence
        self._ocr_cache = None
        self._ocr_scorer = None
        if scorer is not None:
            from coding4game_gui.benchmark.windows.scorers.ocr import OcrScorer
            if isinstance(scorer, OcrScorer):
                from coding4game_gui.benchmark.windows.scorers.perception_cache import EpisodeOcrCache
                try:
                    self._ocr_cache = EpisodeOcrCache(scorer.ocr)
                except BaseException:
                    self.stream.close()
                    raise
                self._ocr_scorer = scorer
                scorer.ocr = self._ocr_cache

    def add(self, frame, *, tics=None):
        if self.settled or self.error is not None:
            raise RuntimeError('episode score already settled')
        ok, data = cv2.imencode('.png', frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise RuntimeError('could not preserve scoring frame')
        raw = data.tobytes()
        self.offsets.append(self.stream.tell())
        self.frame_tics.append(tics)
        self.stream.write(struct.pack('!I', len(raw)))
        self.stream.write(raw)
        self.frame_count += 1

    def settle(self, scorer):
        if self.error is not None:
            raise RuntimeError('episode scoring previously failed') from self.error
        if self.settled:
            return self.value
        try:
            settlement = getattr(scorer, 'spec', {}).get('settlement')
            if settlement in {'latest_valid_frame', 'final_frame'}:
                return self._settle_latest(scorer, final_only=settlement == 'final_frame')
            started = last_report = time.monotonic()
            processed = 0
            print(f"[scoring] Settling {self.frame_count} saved frames; gameplay is stopped.", flush=True)
            scorer.reset()
            self.stream.seek(0)
            total = 0.0
            previous_tic = 0
            best_evidence_total = float('-inf')
            if self.retain_replay_evidence:
                self.details.update(method='ordered_replay', frame_count=self.frame_count,
                                    frame_tics=list(self.frame_tics),
                                    evidence_selection='highest_replayed_total',
                                    sample_protocol='passive_held_action_v1')
            note_tics = getattr(scorer, 'note_tics', None)
            while header := self.stream.read(4):
                size = struct.unpack('!I', header)[0]
                raw = self.stream.read(size)
                if len(raw) != size:
                    raise RuntimeError('truncated scoring evidence')
                frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise RuntimeError('invalid scoring evidence')
                frame_tic = self.frame_tics[processed]
                if frame_tic is not None:
                    if callable(note_tics):
                        note_tics(max(0, frame_tic - previous_tic))
                    previous_tic = frame_tic
                total += float(scorer.delta(frame))
                if self.retain_replay_evidence and total > best_evidence_total:
                    best_evidence_total = total
                    self.selected_png = raw
                    reading = getattr(scorer, 'last_reading', None)
                    self.details.update(selected_frame_index=processed,
                                        selected_frame_tic=frame_tic,
                                        reading=asdict(reading) if is_dataclass(reading) else None)
                processed += 1
                now = time.monotonic()
                if now - last_report >= 10:
                    print(f"[scoring] {processed}/{self.frame_count} frames, "
                          f"{now - started:.1f}s elapsed.", flush=True)
                    last_report = now
            finalize = getattr(scorer, 'finalize', None)
            if callable(finalize):
                final_delta = finalize()
                if final_delta is not None:
                    total += float(final_delta)
            self.value = total
            self.settled = True
            if self.retain_replay_evidence:
                self.details.update(status='scored', score=total)
            print(f"[scoring] Complete: {processed} frames in {time.monotonic() - started:.1f}s.", flush=True)
            return total
        except BaseException as exc:
            self.error = exc
            raise
        finally:
            self.close()

    def _settle_latest(self, scorer, *, final_only=False):
        started = last_report = time.monotonic()
        self.details = dict(method='final_frame' if final_only else 'latest_valid_frame', frame_count=self.frame_count,
                            selected_frame_index=None, skipped_frames=0)
        if final_only:
            print('[scoring] Reading the final saved frame; gameplay is stopped.', flush=True)
        else:
            print(f'[scoring] Searching backward through {self.frame_count} saved frames.', flush=True)
        indices = (range(max(0, self.frame_count - 1), self.frame_count) if final_only
                   else range(self.frame_count - 1, -1, -1))
        for index in indices:
            self.stream.seek(self.offsets[index])
            header = self.stream.read(4)
            if len(header) != 4:
                raise RuntimeError('truncated scoring evidence')
            size = struct.unpack('!I', header)[0]
            raw = self.stream.read(size)
            if len(raw) != size:
                raise RuntimeError('truncated scoring evidence')
            frame = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                raise RuntimeError('invalid scoring evidence')
            if final_only:
                self.endpoint_png = raw
                self.details.update(endpoint_frame_index=index,
                                    endpoint_frame_tic=self.frame_tics[index])
            reading = scorer.snapshot(frame)
            if reading is not None:
                self.value = float(reading['score'])
                self.selected_png = raw
                self.details.update(status='scored', selected_frame_index=index,
                                    selected_frame_tic=self.frame_tics[index], reading=reading)
                break
            self.details['skipped_frames'] += 1
            now = time.monotonic()
            if now - last_report >= 10:
                print(f"[scoring] Skipped {self.details['skipped_frames']} unreadable frames; searching backward.", flush=True)
                last_report = now
        else:
            self.details['status'] = 'no_valid_frame'
        self.settled = True
        print(f"[scoring] {self.details['status']}; score={self.value}; "
              f"skipped={self.details['skipped_frames']}; {time.monotonic() - started:.1f}s.", flush=True)
        return self.value

    def close(self):
        self.stream.close()
        if self._ocr_cache is not None:
            cache, self._ocr_cache = self._ocr_cache, None
            self.details['ocr_reuse'] = dict(hits=cache.hits, misses=cache.misses)
            self._ocr_scorer.ocr = cache.engine
            cache.close()
