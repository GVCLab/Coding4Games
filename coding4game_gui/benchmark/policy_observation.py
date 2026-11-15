"""One visual contract for every benchmark model image, independent of engine."""
from __future__ import annotations

from io import BytesIO

import cv2
import numpy as np
from PIL import Image
from PIL import UnidentifiedImageError

POLICY_WIDTH = 1280
POLICY_HEIGHT = 768
POLICY_SIZE = (POLICY_WIDTH, POLICY_HEIGHT)
POLICY_JPEG_QUALITY = 85
POLICY_MEDIA_TYPE = "image/jpeg"
MODEL_IMAGE_HISTORY_MAX_COUNT = 16
MODEL_IMAGE_HISTORY_MAX_BYTES = 8 * 1024 * 1024


def _model_image_payload_bytes(block: object) -> int | None:
    """Return an image block's provider payload size, or ``None`` if text.

    Coach requests use OpenAI-style ``image_url`` blocks while Lang Player
    requests use Anthropic-style base64 ``image`` blocks.  Converting the
    latter to its equivalent data URI makes the byte cap identical for both.
    """
    if not isinstance(block, dict):
        return None
    if block.get("type") == "image_url":
        image_url = block.get("image_url")
        uri = image_url.get("url") if isinstance(image_url, dict) else None
        return len(uri.encode("utf-8")) if isinstance(uri, str) else 0
    if block.get("type") == "image":
        source = block.get("source")
        if not isinstance(source, dict) or source.get("type") != "base64":
            return 0
        data = source.get("data")
        media_type = source.get("media_type")
        if not isinstance(data, str):
            return 0
        if not isinstance(media_type, str):
            media_type = "application/octet-stream"
        uri = f"data:{media_type};base64,{data}"
        return len(uri.encode("utf-8"))
    return None


def window_model_images(
        messages: list[dict], *,
        max_images: int = MODEL_IMAGE_HISTORY_MAX_COUNT,
        max_image_bytes: int = MODEL_IMAGE_HISTORY_MAX_BYTES,
        omitted_text: str = (
            "[{count} earlier image attachment(s) omitted by the shared "
            "model image window.]"),
        ) -> tuple[list[dict], int, int]:
    """Build a request copy containing only the newest image attachments.

    Text and assistant history remain intact and ``messages`` is never mutated.
    Both the image-count limit and the equivalent data-URI byte limit apply.
    ``omitted_text`` may use ``{count}`` for the number removed from one message.
    """
    max_images = max(0, int(max_images))
    max_image_bytes = max(0, int(max_image_bytes))
    keep: set[tuple[int, int]] = set()
    kept_bytes = 0

    for msg_i in range(len(messages) - 1, -1, -1):
        content = messages[msg_i].get("content")
        if not isinstance(content, list):
            continue
        for block_i in range(len(content) - 1, -1, -1):
            payload_bytes = _model_image_payload_bytes(content[block_i])
            if payload_bytes is None:
                continue
            if (len(keep) < max_images
                    and kept_bytes + payload_bytes <= max_image_bytes):
                keep.add((msg_i, block_i))
                kept_bytes += payload_bytes

    windowed: list[dict] = []
    omitted = 0
    for msg_i, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, list):
            windowed.append(message)
            continue
        new_content = []
        omitted_here = 0
        for block_i, block in enumerate(content):
            if (_model_image_payload_bytes(block) is not None
                    and (msg_i, block_i) not in keep):
                omitted += 1
                omitted_here += 1
                continue
            new_content.append(block)
        if omitted_here:
            new_content.append({
                "type": "text",
                "text": omitted_text.format(count=omitted_here),
            })
        copied = dict(message)
        copied["content"] = new_content
        windowed.append(copied)
    return windowed, omitted, kept_bytes


def policy_frame(frame: np.ndarray) -> np.ndarray:
    """Return the exact BGR/RGB array shape every Code policy receives.

    The harness deliberately uses an exact resize rather than letterboxing:
    1280x768 is the experiment contract even for non-16:9 source games.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3:
        raise ValueError("policy observation must be an HxWxC numpy array")
    if frame.shape[0] == POLICY_HEIGHT and frame.shape[1] == POLICY_WIDTH:
        return frame
    shrinking = (frame.shape[1] > POLICY_WIDTH
                 or frame.shape[0] > POLICY_HEIGHT)
    interpolation = cv2.INTER_AREA if shrinking else cv2.INTER_LINEAR
    return cv2.resize(frame, POLICY_SIZE, interpolation=interpolation)


def encode_policy_image(
        frame: np.ndarray | bytes | bytearray | memoryview) -> tuple[bytes, str]:
    """Decode if needed, then resize/JPEG-encode one model-call image.

    This is the single shared encoder for benchmark Coach and Player image
    attachments. Code policies still consume ``policy_frame`` arrays directly.
    """
    if isinstance(frame, (bytes, bytearray, memoryview)):
        raw = bytes(frame)
        source = np.frombuffer(raw, dtype=np.uint8)
        decoded = (cv2.imdecode(source, cv2.IMREAD_COLOR)
                   if source.size else None)
        if decoded is None:
            try:
                with Image.open(BytesIO(raw)) as image:
                    rgb = np.asarray(image.convert("RGB"))
                decoded = np.ascontiguousarray(rgb[:, :, ::-1])
            except (OSError, UnidentifiedImageError, ValueError):
                raise ValueError("model image bytes could not be decoded") from None
        frame = decoded
    resized = policy_frame(frame)
    ok, encoded = cv2.imencode(
        ".jpg", resized,
        [int(cv2.IMWRITE_JPEG_QUALITY), POLICY_JPEG_QUALITY],
    )
    if not ok:
        raise RuntimeError("failed to encode model image as JPEG")
    return encoded.tobytes(), POLICY_MEDIA_TYPE
