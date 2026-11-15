"""Versioned terminal confirmation and bounded lossless native evidence."""
from collections import deque
import hashlib
import json
from pathlib import Path
import queue
import threading
import time

import cv2

SAMPLING_VERSION = 'during_action_v2'
RECOGNITION_VERSION = 'overcooked_results_v2'


class EvidenceFailure(RuntimeError):
    pass


class NativeEvidence:
    """One bounded FIFO writer; enqueue errors and worker errors fail closed."""
    def __init__(self, directory, uid, capacity=32):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.uid = uid
        self.queue = queue.Queue(maxsize=capacity)
        self.error = None
        self.closed = False
        self.saved = set()
        self.thread = threading.Thread(target=self._write, daemon=True)
        self.thread.start()

    def check(self):
        if self.error:
            raise EvidenceFailure(f'native evidence failed: {self.error}') from self.error

    def add(self, sample, reason):
        self.check()
        if sample['frame_id'] in self.saved:
            return
        if self.closed:
            raise EvidenceFailure('native evidence writer already closed')
        try:
            self.queue.put_nowait((sample, reason))
        except queue.Full as exc:
            self.error = exc
            raise EvidenceFailure('native evidence queue overflow') from exc
        self.saved.add(sample['frame_id'])

    def _write(self):
        try:
            with (self.directory / 'frames.jsonl').open('a', encoding='utf-8') as manifest:
                while True:
                    item = self.queue.get()
                    if item is None:
                        break
                    sample, reason = item
                    frame = sample['frame']
                    path = self.directory / f"frame_{sample['frame_id']:07d}.png"
                    if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_PNG_COMPRESSION, 1]):
                        raise EvidenceFailure('PNG encoding/write failed')
                    raw = path.read_bytes()
                    record = {k: v for k, v in sample.items() if k != 'frame'}
                    record.update(episode_uid=self.uid, sampling_version=SAMPLING_VERSION,
                                  recognition_version=RECOGNITION_VERSION, reason=reason,
                                  dimensions=[frame.shape[1], frame.shape[0]],
                                  sha256=hashlib.sha256(raw).hexdigest(), path=path.name)
                    manifest.write(json.dumps(record, ensure_ascii=False) + '\n')
                    manifest.flush()
        except BaseException as exc:
            self.error = exc

    def close(self):
        if not self.closed:
            self.closed = True
            while self.thread.is_alive():
                try:
                    self.queue.put(None, timeout=.05)
                    break
                except queue.Full:
                    pass
            self.thread.join()
        self.check()


class Stability:
    """Independent capture IDs and monotonic capture times, never game tics."""
    def __init__(self):
        self.last_id = None
        self.last_time = None
        self.key = None
        self.first_time = None
        self.count = 0
        self.confirmed = False

    def update(self, analysis, frame_id, timestamp):
        duplicate = self.last_id is not None and frame_id <= self.last_id
        if duplicate:
            return dict(confirmed=self.confirmed, stable_count=self.count,
                        stable_span=0 if self.first_time is None else self.last_time-self.first_time,
                        reason='duplicate_or_out_of_order_capture', independent=False)
        if self.last_time is not None and timestamp <= self.last_time:
            raise ValueError('capture timestamps must increase')
        self.last_id, self.last_time = frame_id, timestamp
        if not analysis['complete']:
            self.key, self.first_time, self.count = None, None, 0
        else:
            key = tuple(sorted(analysis['values'].items()))
            if key != self.key:
                self.key, self.first_time, self.count = key, timestamp, 0
            self.count += 1
        span = 0 if self.first_time is None else timestamp - self.first_time
        # A confirmed decision is latched; later map frames cannot overwrite it.
        self.confirmed |= self.count >= 3 and span >= .5 - 1e-9
        return dict(confirmed=self.confirmed, stable_count=self.count,
                    stable_span=span, independent=True,
                    reason='confirmed' if self.confirmed else 'incomplete_or_not_stable')


class TerminalMonitor:
    def __init__(self, scorer, directory, uid, tics_per_second=10):
        self.scorer = scorer
        self.writer = NativeEvidence(directory, uid)
        self.stability = Stability()
        self.ring = deque(maxlen=24)
        self.frame_id = 0
        self.last_check = None
        self.candidate = False
        self.ever_candidate = False
        self.unreadable = False
        self.confirmed_frame = None
        self.checks = 0
        self.tics_per_second = tics_per_second
        self.closed = False
        self.failure = None
        self.failure_kind = None
        try:
            self.log = (self.writer.directory / 'checks.jsonl').open('a', encoding='utf-8')
        except BaseException:
            self.writer.close()
            raise

    def capture(self, frame, *, timestamp=None, source, action_id=None, batch_id=None,
                tic=None, force=False):
        if self.closed:
            raise EvidenceFailure('terminal monitor already closed')
        try:
            self.writer.check()
            now = time.perf_counter() if timestamp is None else timestamp
            self.frame_id += 1
            sample = dict(frame=frame.copy(), frame_id=self.frame_id,
                          captured_monotonic=now, source=source, action_id=action_id,
                          batch_id=batch_id, tic=tic)
            self.ring.append(sample)
            while self.ring and now - self.ring[0]['captured_monotonic'] > 2:
                self.ring.popleft()
            if self.stability.confirmed:
                self.writer.add(sample, 'final_frozen' if source == 'frozen_action_frame' else 'after_confirmation')
                return True
            interval = (1 if self.candidate else 3) / self.tics_per_second
            if not force and self.last_check is not None and now-self.last_check < interval - 1e-9:
                if self.candidate:
                    self.writer.add(sample, 'candidate_sampling')
                return False
            actual_interval = None if self.last_check is None else now-self.last_check
            self.last_check = now
            started = time.perf_counter()
            analysis = self.scorer.inspect_results(frame)
            elapsed = time.perf_counter() - started
            self.candidate = analysis['candidate']
            self.ever_candidate |= self.candidate
            self.unreadable |= analysis['diagnostic'] == 'complete_structure_values_unreadable'
            decision = self.stability.update(analysis, self.frame_id, now)
            if self.candidate or self.ever_candidate:
                for recent in self.ring:
                    self.writer.add(recent, 'candidate_or_context')
            self.checks += 1
            record = {k: v for k, v in sample.items() if k != 'frame'}
            record.update(episode_uid=self.writer.uid, sampling_version=SAMPLING_VERSION,
                          recognition_version=RECOGNITION_VERSION, analysis=analysis,
                          decision=decision, requested_interval_s=interval,
                          actual_interval_s=actual_interval, recognition_seconds=elapsed)
            self.log.write(json.dumps(record, ensure_ascii=False) + '\n')
            self.log.flush()
            if decision['confirmed']:
                self.confirmed_frame = sample
            return decision['confirmed']
        except BaseException as exc:
            self.failure = f'{type(exc).__name__}: {exc}'
            self.failure_kind = ('evidence_save_failed' if isinstance(exc, (EvidenceFailure, OSError)) else 'recognition_failed')
            raise

    def close(self):
        if not self.closed:
            try:
                for sample in self.ring:
                    self.writer.add(sample, 'final_buffer')
                self.writer.close()
            except BaseException as exc:
                self.failure = f'{type(exc).__name__}: {exc}'
                self.failure_kind = 'evidence_save_failed'
                raise
            finally:
                try:
                    self.writer.close()
                finally:
                    self.closed = True
                    self.log.close()
                    summary = self.summary()
                    (self.writer.directory / 'summary.json').write_text(
                        json.dumps(summary, indent=2), encoding='utf-8')
        self.writer.check()
        return self.summary()

    def summary(self):
        return dict(episode_uid=self.writer.uid, sampling_version=SAMPLING_VERSION,
                    recognition_version=RECOGNITION_VERSION, checks=self.checks,
                    captured_frames=self.frame_id, evidence_failure=self.failure,
                    confirmed_frame_id=None if self.confirmed_frame is None else self.confirmed_frame['frame_id'],
                    diagnostic=(self.failure_kind if self.failure else
                                'confirmed' if self.stability.confirmed else
                                'complete_structure_values_unreadable' if self.unreadable else
                                'candidate_incomplete' if self.ever_candidate else 'no_candidate_observed'))
