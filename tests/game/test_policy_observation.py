"""The policy-facing visual contract is global and engine-independent."""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest


def test_policy_frame_is_exactly_1280_by_768_for_every_source_shape():
    from coding4game_gui.benchmark.policy_observation import policy_frame

    for shape in ((1536, 2560, 3), (480, 640, 3), (200, 1000, 3)):
        source = np.zeros(shape, dtype=np.uint8)
        assert policy_frame(source).shape == (768, 1280, 3)


def test_encoded_policy_image_is_small_jpeg_at_contract_size():
    from coding4game_gui.benchmark.policy_observation import encode_policy_image

    rng = np.random.default_rng(7)
    source = rng.integers(0, 256, (1536, 2560, 3), dtype=np.uint8)
    encoded, media_type = encode_policy_image(source)
    decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
    ok, original_png = cv2.imencode(".png", source)

    assert media_type == "image/jpeg"
    assert decoded.shape == (768, 1280, 3)
    assert ok and len(encoded) < len(original_png)


def test_encoded_image_bytes_use_the_same_model_image_contract():
    from coding4game_gui.benchmark.policy_observation import encode_policy_image

    source = np.full((360, 640, 3), (10, 80, 200), dtype=np.uint8)
    ok, png = cv2.imencode(".png", source)
    assert ok

    encoded, media_type = encode_policy_image(png.tobytes())
    decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8),
                           cv2.IMREAD_COLOR)

    assert media_type == "image/jpeg"
    assert decoded.shape == (768, 1280, 3)


def test_model_image_encoder_uses_shared_jpeg_quality(monkeypatch):
    from coding4game_gui.benchmark import policy_observation

    seen = {}
    real_imencode = cv2.imencode

    def capture(ext, frame, params):
        seen.update(ext=ext, shape=frame.shape, params=params)
        return real_imencode(ext, frame, params)

    monkeypatch.setattr(policy_observation.cv2, "imencode", capture)
    policy_observation.encode_policy_image(
        np.zeros((100, 200, 3), dtype=np.uint8))

    assert seen == {
        "ext": ".jpg",
        "shape": (768, 1280, 3),
        "params": [int(cv2.IMWRITE_JPEG_QUALITY), 85],
    }


def test_model_image_encoder_rejects_undecodable_bytes():
    from coding4game_gui.benchmark.policy_observation import encode_policy_image

    with pytest.raises(ValueError, match="could not be decoded"):
        encode_policy_image(b"not an image")


def test_model_image_window_supports_coach_and_player_blocks_without_mutation():
    from coding4game_gui.benchmark.policy_observation import window_model_images

    messages = []
    for i in range(3):
        messages.append({"role": "assistant", "content": f"reply {i}"})
        messages.append({"role": "user", "content": [
            {"type": "text", "text": f"status {i}"},
            ({"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,coach-{i}"}}
             if i % 2 == 0 else
             {"type": "image", "source": {
                 "type": "base64", "media_type": "image/jpeg",
                 "data": f"player-{i}"}}),
        ]})
    before = json.dumps(messages, sort_keys=True)

    windowed, omitted, kept_bytes = window_model_images(
        messages, max_images=2, max_image_bytes=10_000)

    assert json.dumps(messages, sort_keys=True) == before
    assert omitted == 1
    rendered = json.dumps(windowed)
    assert "coach-0" not in rendered
    assert "player-1" in rendered
    assert "coach-2" in rendered
    assert all(f"reply {i}" in rendered and f"status {i}" in rendered
               for i in range(3))
    expected = [
        "data:image/jpeg;base64,player-1",
        "data:image/jpeg;base64,coach-2",
    ]
    assert kept_bytes == sum(len(value.encode("utf-8")) for value in expected)


def test_model_image_window_enforces_total_payload_bytes():
    from coding4game_gui.benchmark.policy_observation import window_model_images

    def player_message(label: str, size: int) -> dict:
        return {"role": "user", "content": [
            {"type": "text", "text": label},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": label * size}},
        ]}

    messages = [player_message("a", 20), player_message("b", 20),
                player_message("c", 20)]
    newest_payload = "data:image/jpeg;base64," + "c" * 20
    windowed, omitted, kept_bytes = window_model_images(
        messages, max_images=10,
        max_image_bytes=len(newest_payload.encode("utf-8")))

    images = [block for message in windowed for block in message["content"]
              if block.get("type") == "image"]
    assert len(images) == 1
    assert images[0]["source"]["data"] == "c" * 20
    assert omitted == 2
    assert kept_bytes == len(newest_payload.encode("utf-8"))

