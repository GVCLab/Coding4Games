"""Direct player request and image formatting."""
from __future__ import annotations

import base64

class PlayerInfrastructureError(RuntimeError):
    """A player call failed outside the model-policy/action contract."""

    def __init__(self, message: str, *, attempts: int = 1):
        super().__init__(message)
        self.attempts = int(attempts)


def image_block(image_bytes: bytes, media_type: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                        "data": base64.b64encode(image_bytes).decode()}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def call_llm(model: str, system: str, messages: list[dict],
             max_tokens: int = 4096, **request_kwargs) -> tuple[str, int, int]:
    from coding4game_gui.benchmark.direct.provider import request_message

    resp = request_message(
        model=model, system=system, messages=messages, max_tokens=max_tokens,
        **request_kwargs)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    u = resp.usage
    return text, int(u.input_tokens), int(u.output_tokens)
