"""Episode-local, disk-backed reuse of exact-frame OCR results."""
import hashlib
import json
import sqlite3
import tempfile
import threading
from pathlib import Path


class EpisodeOcrCache:
    """Wrap one unchanged OCR engine; never retain images or score state."""

    def __init__(self, engine):
        self.engine = engine
        self.hits = self.misses = 0
        self._lock = threading.RLock()
        self._directory = tempfile.TemporaryDirectory(prefix='c4g-ocr-')
        try:
            self._db = sqlite3.connect(
                str(Path(self._directory.name) / 'perception.sqlite'),
                check_same_thread=False, isolation_level=None)
            # This disposable cache is not the authoritative episode evidence.
            self._db.execute('PRAGMA journal_mode=OFF')
            self._db.execute('PRAGMA synchronous=OFF')
            self._db.execute('PRAGMA cache_size=-2048')
            self._db.execute('CREATE TABLE ocr (key BLOB PRIMARY KEY, items TEXT)')
        except BaseException:
            if hasattr(self, '_db'):
                self._db.close()
            self._directory.cleanup()
            raise

    def __call__(self, frame):
        return self._read(frame, 'detect', {}, lambda: self.engine(frame))

    def recognize(self, frame, *, trim_light_text=False):
        return self._read(
            frame, 'recognize', {'trim_light_text': bool(trim_light_text)},
            lambda: self.engine.recognize(frame, trim_light_text=trim_light_text))

    def _read(self, frame, operation, options, infer):
        digest = hashlib.sha256()
        digest.update(json.dumps(
            [operation, options, frame.shape, frame.dtype.str],
            sort_keys=True).encode('ascii'))
        digest.update(frame.tobytes())
        key = digest.digest()
        with self._lock:
            row = self._db.execute('SELECT items FROM ocr WHERE key=?', (key,)).fetchone()
            if row is not None:
                self.hits += 1
                return json.loads(row[0])
            items = infer()
            encoded = json.dumps(items, ensure_ascii=False, allow_nan=False)
            self._db.execute('INSERT INTO ocr VALUES (?, ?)', (key, encoded))
            self.misses += 1
            return items

    def close(self):
        with self._lock:
            self._db.close()
            self._directory.cleanup()
