"""Native-viewport image assertions for a frozen startup handoff."""
from __future__ import annotations

import math
from pathlib import Path

import cv2
from coding4game_gui.paths import REPO_ROOT


class StartupImageChecks:
    """Load explicit UI references once and require every measured region."""

    def __init__(self, checks, client_size, *, root=None):
        root = Path(root or REPO_ROOT).resolve()
        if not client_size or len(client_size) != 2:
            raise ValueError('image start checks require calibrated_client_size')
        self.size = tuple(int(v) for v in client_size)
        if min(self.size) <= 0 or not isinstance(checks, list) or not checks:
            raise ValueError('image start checks require a positive viewport and nonempty list')
        self.regions = []
        for item in checks:
            if not isinstance(item, dict):
                raise ValueError('each image start check must be a mapping')
            raw = item.get('path')
            if not isinstance(raw, str) or not raw or Path(raw).is_absolute():
                raise ValueError('image start reference must be repository-relative')
            path = (root / raw).resolve()
            if not path.is_relative_to(root):
                raise ValueError('image start reference must remain inside the repository')
            image = cv2.imread(str(path))
            if image is None:
                raise ValueError(f'image start reference is unreadable: {raw}')
            if (image.shape[1], image.shape[0]) != self.size:
                raise ValueError('image start reference must match calibrated_client_size')
            rect = item.get('rect')
            if (not isinstance(rect, list) or len(rect) != 4
                    or any(type(v) is not int for v in rect)):
                raise ValueError('image start rect must contain four integers')
            x, y, w, h = rect
            if (min(x, y) < 0 or min(w, h) <= 0
                    or x + w > self.size[0] or y + h > self.size[1]):
                raise ValueError('image start rect is outside the calibrated viewport')
            reference = image[y:y+h, x:x+w].copy()
            if 'min_correlation' in item:
                limit = item['min_correlation']
                if ('max_mean_abs_error' in item or isinstance(limit, bool)
                        or not isinstance(limit, (int, float))
                        or not math.isfinite(limit) or not 0 < limit <= 1):
                    raise ValueError('image correlation must be finite within (0,1] and exclusive of mean error')
                margin = item.get('search_margin', [0, 0])
                if (not isinstance(margin, list) or len(margin) != 2
                        or any(type(v) is not int or v < 0 for v in margin)):
                    raise ValueError('image search margin requires two nonnegative integers')
                mx, my = margin
                if (x < mx or y < my or x+w+mx > self.size[0]
                        or y+h+my > self.size[1]):
                    raise ValueError('image search margin is outside the native viewport')
                angles = item.get('rotations', [0])
                if (not isinstance(angles, list) or not 1 <= len(angles) <= 21
                        or any(isinstance(a, bool) or not isinstance(a, (int, float))
                               or not math.isfinite(a) or abs(a) > 30 for a in angles)):
                    raise ValueError('image rotations require 1..21 finite angles within -30..30')
                if float(reference.std()) == 0:
                    raise ValueError('image correlation reference needs visible texture')
                variants = [cv2.warpAffine(reference,
                    cv2.getRotationMatrix2D((w/2, h/2), angle, 1), (w, h),
                    borderMode=cv2.BORDER_REPLICATE) for angle in angles]
                self.regions.append((rect, reference, float(limit), variants, margin))
                continue
            limit = item.get('max_mean_abs_error')
            if (isinstance(limit, bool) or not isinstance(limit, (int, float))
                    or not math.isfinite(limit) or not 0 <= limit <= 255):
                raise ValueError('image start error limit must be finite and within 0..255')
            self.regions.append((rect, reference, float(limit), None, [0, 0]))

    def matches(self, frame) -> bool:
        if (frame.shape[1], frame.shape[0]) != self.size:
            return False
        for (x, y, w, h), reference, limit, variants, (mx, my) in self.regions:
            if variants is not None:
                search = frame[y-my:y+h+my, x-mx:x+w+mx]
                if not any(cv2.minMaxLoc(cv2.matchTemplate(
                        search, variant, cv2.TM_CCOEFF_NORMED))[1] >= limit
                           for variant in variants):
                    return False
                continue
            error = float(cv2.absdiff(frame[y:y+h, x:x+w], reference).mean())
            if error > limit:
                return False
        return True
