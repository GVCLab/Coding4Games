"""In-process LiteLLM transport for the stepped Direct player."""
from __future__ import annotations

import asyncio
import os
import random
import re
import time
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Optional
from typing import TypeVar

import litellm
import yaml
from anthropic.types import Message


from coding4game_gui.operator_config import operator_paths

_routes: dict[str, dict] = {}
_loaded = False

# Keep LiteLLM's own retry loop disabled so every attempt is observable. Temporary
# upstream failures are bounded by a wall-clock recovery window rather than a
# small fixed attempt count: fast 429/concurrency failures must get the same
# chance to recover as slow connection timeouts.
_DEFAULT_TIMEOUT = float(os.environ.get("C4G_LLM_TIMEOUT", "240"))
_DEFAULT_RETRY_WINDOW_S = float(
    os.environ.get("C4G_LLM_RETRY_WINDOW", "1200"))
_RETRIES_OVERRIDE = os.environ.get("C4G_LLM_RETRIES")
_DEFAULT_NUM_RETRIES = (
    None if _RETRIES_OVERRIDE in (None, "")
    else max(0, int(_RETRIES_OVERRIDE)))
_RETRY_BASE_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 60.0
_DEADLINE_EPSILON_S = 0.001

_T = TypeVar("_T")
RetryObserver = Callable[[dict[str, Any]], None]

_TRANSIENT_ERROR_MARKERS = (
    "upstream provider error", "rate limit", "too many requests",
    "concurrency limit", "per-account concurrency", "apierror", "api error",
    "timeout", "timed out", "apiconnectionerror", "connection error",
    "connectionerror", "connection aborted", "connection reset",
    "connectionabortederror", "connectionreseterror",
    "errno 10053", "winerror 10053", "errno 10054", "winerror 10054",
    "service unavailable", "temporarily unavailable", "internal server error",
    "internalservererror", "bad gateway", "gateway timeout",
)
_PERMANENT_ERROR_MARKERS = (
    "余额不足", "insufficient balance", "missing credentials",
    "invalid api key", "invalid_api_key", "authentication", "unauthorized",
    "forbidden", "permission denied", "invalid request",
    "invalid_request_error", "context length", "context window",
    "context_window", "maximum context", "prompt is too long",
    "quota exceeded", "billing", "validationerror",
)


def _notify(observer: RetryObserver | None, event: str, **fields: Any) -> None:
    """Best-effort request telemetry; observers must never break transport."""
    if observer is None:
        return
    try:
        observer({"event": event, **fields})
    except Exception:  # noqa: BLE001
        pass


def _status_code(exc: Exception) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        value = getattr(obj, "status_code", None)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:status(?: code)?\s*[:=]?\s*)(\d{3})",
                      f"{type(exc).__name__}: {exc}", re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_transient_error(exc: Exception) -> bool:
    """Return whether repeating the exact request can plausibly recover."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in _PERMANENT_ERROR_MARKERS):
        return False
    status = _status_code(exc)
    if status is not None:
        return status in (408, 409, 429) or 500 <= status <= 599
    return any(marker in text for marker in _TRANSIENT_ERROR_MARKERS)


def _header_value(exc: Exception, name: str) -> str | None:
    wanted = name.lower()
    for obj in (getattr(exc, "response", None), exc):
        headers = getattr(obj, "headers", None) if obj is not None else None
        if headers is None:
            continue
        try:
            for key, value in headers.items():
                if str(key).lower() == wanted:
                    return str(value)
        except Exception:  # noqa: BLE001
            continue
    return None


def _retry_after_seconds(exc: Exception, *, now: datetime | None = None,
                         ) -> float | None:
    raw = _header_value(exc, "retry-after")
    if raw is None:
        match = re.search(r"retry-after\s*[:=]\s*([^,;\s]+)", str(exc),
                          re.IGNORECASE)
        raw = match.group(1) if match else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            current = now or datetime.now(timezone.utc)
            return max(0.0, (target - current).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _retry_delay(exc: Exception, retry_number: int) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    base = min(_RETRY_BASE_DELAY_S * (2 ** max(0, retry_number - 1)),
               _RETRY_MAX_DELAY_S)
    return random.uniform(base * 0.8, base * 1.2)


def _attempt_timeout(timeout: float | None, deadline: float | None) -> float | None:
    if deadline is None:
        return timeout
    remaining = max(0.001, deadline - time.monotonic())
    return remaining if timeout is None else min(timeout, remaining)


def _effective_retry_deadline(deadline: float | None,
                              retry_window_s: float | None,
                              ) -> tuple[float, float]:
    """Return the absolute retry deadline and effective window from now."""
    started = time.monotonic()
    window = (_DEFAULT_RETRY_WINDOW_S if retry_window_s is None
              else float(retry_window_s))
    if window < 0:
        raise ValueError("retry_window_s must be non-negative")
    effective = started + window
    if deadline is not None:
        effective = min(effective, float(deadline))
    return effective, max(0.0, effective - started)


def _max_attempts(max_retries: int | None) -> int | None:
    retries = _DEFAULT_NUM_RETRIES if max_retries is None else max_retries
    return None if retries is None else max(0, int(retries)) + 1


def _event_fields(attempt: int, max_attempts: int | None,
                  deadline: float, retry_window_s: float) -> dict[str, Any]:
    return {
        "attempt": attempt,
        "max_attempts": max_attempts,
        "retry_window_s": retry_window_s,
        "remaining_s": max(0.0, deadline - time.monotonic()),
    }


async def request_with_retry_async(
        operation: Callable[[float | None], Awaitable[_T]], *,
        timeout: float | None = None, max_retries: int | None = None,
        deadline: float | None = None,
        retry_window_s: float | None = None,
        observer: RetryObserver | None = None) -> _T:
    """Apply the Direct player's bounded retry window to one logical request."""
    max_attempts = _max_attempts(max_retries)
    retry_deadline, retry_window = _effective_retry_deadline(
        deadline, retry_window_s)
    attempt = 0
    while True:
        attempt += 1
        fields = _event_fields(
            attempt, max_attempts, retry_deadline, retry_window)
        _notify(observer, "attempt", **fields)
        try:
            result = await operation(_attempt_timeout(timeout, retry_deadline))
            _notify(observer, "success", **_event_fields(
                attempt, max_attempts, retry_deadline, retry_window))
            return result
        except Exception as exc:  # noqa: BLE001
            transient = is_transient_error(exc)
            fields = _event_fields(
                attempt, max_attempts, retry_deadline, retry_window)
            _notify(observer, "failure", **fields, exception=exc,
                    transient=transient)
            exhausted = max_attempts is not None and attempt >= max_attempts
            if not transient or exhausted:
                _notify(observer, "give_up", **fields, exception=exc,
                        reason="permanent" if not transient else "exhausted")
                raise
            delay = _retry_delay(exc, attempt)
            remaining = retry_deadline - time.monotonic()
            if remaining <= 2 * _DEADLINE_EPSILON_S:
                _notify(observer, "give_up", **_event_fields(
                    attempt, max_attempts, retry_deadline, retry_window),
                    exception=exc, reason="deadline")
                raise
            delay = min(delay, remaining - _DEADLINE_EPSILON_S)
            _notify(observer, "retry", **_event_fields(
                attempt, max_attempts, retry_deadline, retry_window),
                exception=exc, delay_s=delay)
            await asyncio.sleep(delay)


def _resolve_secret(value: Any) -> Any:
    """Resolve `os.environ/VAR` references (LiteLLM config convention)."""
    if isinstance(value, str) and value.startswith("os.environ/"):
        return os.environ.get(value[len("os.environ/"):], "")
    return value


def _load_config(path: Optional[Path] = None) -> None:
    """Parse litellm.config.yaml once: build model_name → route, apply settings."""
    global _loaded
    if _loaded and path is None:
        return
    cfg_path = path or operator_paths()["credentials"]
    _routes.clear()

    # Keep the existing Chat Completions translation for OpenAI-compatible routes.
    litellm.use_chat_completions_url_for_anthropic_messages = True
    # Keep the console clean — the runner has its own logging.
    litellm.suppress_debug_info = True

    if not cfg_path.exists():
        _loaded = True
        return
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    for entry in data.get("model_list", []) or []:
        name = entry.get("model_name")
        params = entry.get("litellm_params", {}) or {}
        if not name:
            continue
        _routes[name] = {
            "model": params.get("model", name),
            "api_base": params.get("api_base"),
            "api_key": _resolve_secret(params.get("api_key")),
        }

    # Apply host settings to the in-process client.
    settings = data.get("litellm_settings", {}) or {}
    if settings.get("drop_params"):
        litellm.drop_params = True
    # Honor an explicit opt-out (`false`) back to the Responses API; otherwise
    # the forced default above stands.
    if "use_chat_completions_url_for_anthropic_messages" in settings:
        litellm.use_chat_completions_url_for_anthropic_messages = bool(
            settings["use_chat_completions_url_for_anthropic_messages"]
        )
    _loaded = True


def _route_kwargs(kwargs: dict) -> dict:
    """Rewrite the Anthropic-format call kwargs with the configured route.

    Looks up the friendly `model` (e.g. "gpt-5.5") in the config's model_list and
    swaps in the provider-prefixed model + api_base + api_key. Unknown models pass
    through unchanged (so a real Anthropic model id still works directly).
    """
    _load_config()
    out = dict(kwargs)
    route = _routes.get(kwargs.get("model"))
    if route:
        out["model"] = route["model"]
        if route.get("api_base") and "api_base" not in out:
            out["api_base"] = route["api_base"]
        if route.get("api_key") and "api_key" not in out:
            out["api_key"] = route["api_key"]
    # Bound a stalled upstream. The shared outer loop owns retries; keeping the
    # SDK at zero makes attempts and billing-visible failures auditable.
    out.setdefault("timeout", _DEFAULT_TIMEOUT)
    out["num_retries"] = 0
    return out


def _request_options(kwargs: dict) -> tuple[
        dict, int | None, float | None, float | None,
        RetryObserver | None]:
    """Remove C4G transport controls before kwargs reach LiteLLM."""
    out = dict(kwargs)
    raw_retries = out.pop("num_retries", _DEFAULT_NUM_RETRIES)
    retries = None if raw_retries is None else max(0, int(raw_retries))
    deadline = out.pop("retry_deadline", None)
    retry_window_s = out.pop("retry_window_s", None)
    observer = out.pop("retry_observer", None)
    return out, retries, deadline, retry_window_s, observer


def _to_message(resp: Any) -> Message:
    """litellm returns an Anthropic-format dict; rebuild the `anthropic` Message
    object so the runner's attribute access (`.content`, `.usage`, ...) works.

    OpenAI-style reasoning has no Anthropic cryptographic signature.  LiteLLM
    represents that mismatch as ``thinking(signature=None)``, while the
    official Anthropic type requires a string and rejects the entire response.
    The runner never consumes or replays thinking blocks, so discard only those
    unsigned blocks and preserve text (including ACT lines) and usage.
    """
    data = dict(resp)
    content = []
    for original in data.get("content") or []:
        if isinstance(original, dict):
            block = dict(original)
        elif hasattr(original, "model_dump"):
            block = original.model_dump()
        else:
            block = original
        if (isinstance(block, dict)
                and block.get("type") == "thinking"
                and not isinstance(block.get("signature"), str)):
            continue
        content.append(block)
    data["content"] = content
    return Message.model_validate(data)


def request_message(**kwargs) -> Message:
    """Call LiteLLM Messages from the synchronous, serial baseline runner."""
    request, retries, deadline, retry_window_s, observer = _request_options(kwargs)
    routed = _route_kwargs(request)
    timeout = routed.pop("timeout", _DEFAULT_TIMEOUT)

    async def send(attempt_timeout: float | None):
        return await litellm.anthropic_interface.messages.acreate(
            **routed, timeout=attempt_timeout)

    response = asyncio.run(request_with_retry_async(
        send, timeout=timeout, max_retries=retries, deadline=deadline,
        retry_window_s=retry_window_s, observer=observer))
    return _to_message(response)
