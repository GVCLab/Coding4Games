"""Narrow, loopback-only model gateway for sandboxed coding-agent CLIs.

The gateway accepts the Codex Responses and Claude Code Messages routes. It
replaces the requested model and keeps the real provider credential on the host.

``token_limit`` is a hard prospective total-token envelope only when
``strict_total_enforcement`` is true (the default) and either ``count_input``
uses a provider that faithfully implements the Responses input-token counting
contract or ``input_token_bound`` supplies a provider-certified upper bound.
The gateway reserves that input count/bound plus the request's output bound
before the call.  Bytes, characters, and base64 lengths are never presented as
token bounds.  In explicitly selected usage-only mode, provider-reported usage
is accounted after a call but is not a hard prospective total-token bound.
Unknown use is never zero and makes remaining tokens unknowable. It remains an
audited warning in usage mode so ordinary client recovery can continue; strict
mode still stops on the first unknown result. Known token thresholds, deadlines
and explicit revocations continue to apply.
Every generation request receives a host-owned system instruction that makes
program delivery the primary outcome and distinguishes the independent token,
tic and wall-clock budgets. After confirmed usage reaches 70% of a finite limit,
the gateway also injects one finalization warning into the next admitted request.
Upstream HTTP 429 responses use a bounded, audited recovery window and never
extend the experiment deadline.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterable
from typing import Mapping
from urllib.parse import urlsplit
from urllib.parse import urlunsplit


_ROUTES = {"/v1/responses", "/v1/responses/compact"}
_ANTHROPIC_ROUTES = {"/v1/messages", "/v1/messages/count_tokens"}
# Large multimodal coding turns can spend several minutes in upstream image
# decoding and inference. Keep the request unchanged and allow the provider up
# to 30 minutes to produce or stream a response.
_REQUEST_TIMEOUT_SECONDS = 1800
_RATE_LIMIT_RECOVERY_SECONDS = 180
_RATE_LIMIT_INITIAL_BACKOFF_SECONDS = 5
_FINALIZATION_WARNING_NUMERATOR = 7
_FINALIZATION_WARNING_DENOMINATOR = 10
_DELIVERY_BUDGET_INSTRUCTION = (
    "Primary objective and hard completion condition: deliver and explicitly select "
    "a runnable game-playing program within the configured finite budgets. The "
    "model-token budget, gameplay-tic budget, and wall-clock budget are independent "
    "hard limits, not one interchangeable pool and not targets to consume. Before "
    "any applicable budget ends, write a runnable candidate, preserve it with "
    "submission.save, obtain a bounded submission.run whose program_returncode is "
    "zero, and call submission.finish(id) on that evaluated candidate. Direct play, "
    "research, screenshots, and calibration are not the deliverable. Remaining "
    "gameplay tics never justify exhausting model tokens before submission. Manage "
    "all budgets from the start, do not defer implementation or submission until the "
    "end, and prefer a weak evaluated program over no selected program."
)
_GLM_CODEX_TOOL_COMPATIBILITY_INSTRUCTION = (
    "Runtime tool compatibility: If apply_patch is absent from the actual tool "
    "list, it is unavailable in this Codex runtime; do not invoke it through a "
    "nested shell or PowerShell command. Use an available direct shell/filesystem "
    "method instead. After two substantially identical file-write failures, change "
    "methods rather than repeating the same command or quoting pattern."
)
_FINALIZATION_WARNING = (
    "Host budget warning: confirmed model-token usage has reached at least 70% "
    "of this run's limit. FINALIZATION IS NOW MANDATORY. Do not perform more optional "
    "research, calibration, capability probing, or open-ended improvement. Inspect "
    "bench.status only if needed, immediately write a minimal runnable fallback if no "
    "candidate exists, preserve it with submission.save, immediately perform the "
    "smallest bounded submission.run needed to obtain program_returncode zero, repair "
    "execution errors if necessary, and call submission.finish(id) on that evaluated "
    "candidate before any other optional work. Weak gameplay performance does not "
    "excuse a no-submission outcome. Unused game tics do not block final selection. "
    "This warning does not extend any budget."
)


class RetryableBeforeSendError(Exception):
    """Transport certifies that the failed attempt sent no bytes upstream."""


class _InputTokenCountError(ValueError):
    """Safe summary only: never includes provider bodies or transport messages."""

    def __init__(self, reason: str, http_status: int | None):
        status = f" (HTTP {http_status})" if http_status is not None else ""
        super().__init__(f"provider input token counting failed: {reason}{status}; "
                         "strict total-token budget remains enabled")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class _UrllibResponse:
    def __init__(self, response):
        self._response = response
        self.status = response.status
        self.headers = dict(response.headers.items())

    def iter_bytes(self) -> Iterable[bytes]:
        reader = getattr(self._response, "read1", self._response.read)
        while True:
            chunk = reader(64 * 1024)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._response.close()


def validate_proxy(mode="system", url=None):
    """Validate before preview/logging; credential-bearing proxy URLs are refused."""
    if mode not in {"system", "direct", "explicit"}:
        raise ValueError("model proxy mode must be system, direct, or explicit")
    if mode != "explicit":
        if url is not None:
            raise ValueError("model proxy URL requires explicit mode")
        return {"mode": mode, "url": None}
    if not isinstance(url, str) or not url or any(c.isspace() for c in url):
        raise ValueError("explicit model proxy requires an HTTP proxy URL")
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("model proxy must be HTTP without credentials, path, query, or fragment")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("invalid model proxy port")
    return {"mode": mode, "url": urlunsplit(("http", parsed.netloc, "", "", ""))}


class _ExplicitProxy(urllib.request.ProxyHandler):
    def proxy_open(self, req, proxy, type):
        # Explicit operator choice must not be silently bypassed by host
        # NO_PROXY/registry overrides. HTTPSHandler still verifies target TLS
        # and uses CONNECT through this HTTP proxy.
        req.set_proxy(urlsplit(proxy).netloc, "http")
        return None


def make_transport(proxy_mode="system", proxy_url=None):
    selected = validate_proxy(proxy_mode, proxy_url)
    handlers = [_NoRedirect()]
    if proxy_mode == "direct":
        handlers.append(urllib.request.ProxyHandler({}))
    elif proxy_mode == "explicit":
        handlers.append(_ExplicitProxy({"http": selected['url'], "https": selected['url']}))
    # System mode intentionally uses urllib's host environment/Windows registry
    # discovery. Nothing here changes process-global environment or registry.
    opener = urllib.request.build_opener(*handlers)

    def transport(method, url, headers, body, timeout):
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            return _UrllibResponse(opener.open(request, timeout=timeout))
        except urllib.error.HTTPError as error:
            return _UrllibResponse(error)
    return transport


def _json_error(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}},
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _retry_after_seconds(headers: Mapping[str, Any]) -> float | None:
    value = next((value for key, value in headers.items()
                  if key.lower() == "retry-after"), None)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())


def _remote_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            if child_key in {"file_id", "file_url"} and isinstance(child, str):
                return True
            if child_key == "image_url" and isinstance(child, str):
                if not child.startswith("data:"):
                    return True
            if _remote_reference(child):
                return True
    elif isinstance(value, list):
        return any(_remote_reference(child) for child in value)
    return False


def _usage_from_json(value: Any) -> int | None:
    if not isinstance(value, Mapping):
        return None
    usage = value.get("usage")
    if isinstance(usage, Mapping):
        total = usage.get("total_tokens")
        if type(total) is int and total >= 0:
            return total
        # Some compatible providers report only the two components. Never
        # coerce strings/bools/negative values or replace an invalid total.
        if "total_tokens" not in usage:
            required = (usage.get("input_tokens"), usage.get("output_tokens"))
            if all(type(n) is int and n >= 0 for n in required):
                cached = tuple(usage.get(name, 0) for name in (
                    "cache_creation_input_tokens", "cache_read_input_tokens"))
                if all(type(n) is int and n >= 0 for n in cached):
                    return sum(required) + sum(cached)
    return _usage_from_json(value.get("response"))


def _usage_components(value, protocol="responses"):
    """Normalize provider counts; absent breakdowns remain unknown, never zero."""
    if not isinstance(value, Mapping):
        return {}
    if isinstance(value.get("response"), Mapping):
        value = value["response"]
    usage = value.get("usage", {})
    if not isinstance(usage, Mapping):
        return {}
    valid = lambda n: type(n) is int and n >= 0
    if protocol == "anthropic":
        uncached = usage.get("input_tokens")
        cached = usage.get("cache_read_input_tokens", 0)
        creation = usage.get("cache_creation_input_tokens", 0)
        uncached = uncached + creation if valid(uncached) and valid(creation) else None
        output = usage.get("output_tokens")
        input_count = uncached + cached if valid(uncached) and valid(cached) else None
    else:
        input_count = usage.get("input_tokens")
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, Mapping) else None
        output = usage.get("output_tokens")
        uncached = input_count - cached if valid(input_count) and valid(cached) else None
    if valid(cached) and valid(input_count) and cached > input_count:
        cached = uncached = None
    return {name: count for name, count in {
        "input_tokens": input_count, "cached_input_tokens": cached,
        "uncached_input_tokens": uncached, "output_tokens": output,
    }.items() if valid(count)}


class ModelGateway:
    """Authenticated host gateway for the selected coding-agent provider."""

    def __init__(
        self,
        upstream_url: str,
        api_key: str,
        model: str,
        token_limit: int | None,
        deadline: float | None,
        log_path: str | Path,
        port: int = 0,
        transport: Callable[..., Any] | None = None,
        *,
        input_token_bound: Callable[[Mapping[str, Any]], int] | None = None,
        count_input: bool = False,
        default_max_output_tokens: int = 4096,
        strict_total_enforcement: bool = True,
        max_request_bytes: int = 64 * 1024 * 1024,
        max_response_bytes: int = 64 * 1024 * 1024,
        max_attempts: int = 2,
        rate_limit_recovery_seconds: float = _RATE_LIMIT_RECOVERY_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        proxy_mode: str = "system",
        proxy_url: str | None = None,
        protocol: str = "responses",
        upstream_api: str = "responses",
        provider_model: str | None = None,
        anthropic_user_id_mode: str = "preserve",
        anthropic_session_id: str | None = None,
        anthropic_tool_replay_mode: str = "preserve",
        anthropic_tool_replay_sources: Iterable[Mapping[str, Any]] = (),
        stop_on_http_400: bool = False,
    ):
        parsed = urlsplit(upstream_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("upstream_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("upstream_url may not contain credentials, query, or fragment")
        if not api_key or not model:
            raise ValueError("api_key and model are required")
        if token_limit is not None and (type(token_limit) is not int or token_limit <= 0):
            raise ValueError("token_limit must be positive or None")
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not 0 < deadline < float('inf')):
            raise ValueError("deadline must be finite and positive or None")
        if token_limit is None and strict_total_enforcement:
            raise ValueError("unlimited tokens require explicit usage accounting")
        if max_request_bytes <= 0 or max_response_bytes <= 0 or max_attempts <= 0:
            raise ValueError("byte limits and max_attempts must be positive")
        if (isinstance(rate_limit_recovery_seconds, bool)
                or not isinstance(rate_limit_recovery_seconds, (int, float))
                or not 0 <= rate_limit_recovery_seconds < float("inf")):
            raise ValueError("rate_limit_recovery_seconds must be finite and non-negative")
        if not callable(sleep):
            raise ValueError("sleep must be callable")
        if type(default_max_output_tokens) is not int or default_max_output_tokens <= 0:
            raise ValueError("default_max_output_tokens must be a positive integer")
        if count_input and input_token_bound is not None:
            raise ValueError("choose either count_input or input_token_bound")
        if type(stop_on_http_400) is not bool:
            raise ValueError("stop_on_http_400 must be boolean")
        if protocol not in {"responses", "anthropic"}:
            raise ValueError("protocol must be responses or anthropic")
        if (anthropic_user_id_mode not in {"preserve", "session_uuid"}
                or anthropic_user_id_mode != "preserve" and protocol != "anthropic"):
            raise ValueError("Invalid Anthropic metadata user_id compatibility mode")
        if anthropic_user_id_mode == "session_uuid":
            try:
                canonical_session = str(uuid.UUID(anthropic_session_id))
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError("Metadata compatibility requires the original session UUID") from error
            if canonical_session != anthropic_session_id:
                raise ValueError("Metadata compatibility requires the canonical original session UUID")
        elif anthropic_session_id is not None:
            raise ValueError("A metadata session is only valid in session_uuid mode")
        if (anthropic_tool_replay_mode not in {"preserve", "restore_leading_cd"}
                or anthropic_tool_replay_mode != "preserve"
                and (protocol != "anthropic" or anthropic_user_id_mode != "session_uuid")):
            raise ValueError("Tool replay compatibility requires the matching Anthropic session")
        if anthropic_tool_replay_mode == "preserve" and anthropic_tool_replay_sources:
            raise ValueError("Tool replay provenance requires explicit compatibility opt-in")
        if upstream_api not in {"responses", "chat"}:
            raise ValueError("upstream_api must be responses or chat")
        if upstream_api == "chat" and protocol != "responses":
            raise ValueError("chat upstream adaptation is only available to the Responses client")
        if upstream_api == "chat" and proxy_mode != "system":
            raise ValueError("chat upstream adaptation currently requires system proxy mode")
        self._upstream = urlunsplit((parsed.scheme, parsed.netloc,
                                    parsed.path.rstrip("/"), "", ""))
        self._api_key = api_key
        self._model = model
        self._token_limit = token_limit if token_limit is not None else float('inf')
        self._deadline_seconds = deadline if deadline is not None else float('inf')
        self._log_path = Path(log_path)
        self._requested_port = port
        self._proxy = validate_proxy(proxy_mode, proxy_url)
        if upstream_api == "chat" and transport is None:
            from coding4game_gui.benchmark.code.chat_responses_adapter import (
                make_chat_responses_transport,
            )
            self._transport = make_chat_responses_transport(
                api_base=self._upstream,
                api_key=api_key,
                provider_model=provider_model or "openai/" + model,
            )
        else:
            self._transport = transport or make_transport(proxy_mode, proxy_url)
        self._input_token_bound = input_token_bound
        self._count_input = count_input
        self._default_max_output_tokens = default_max_output_tokens
        self._strict = strict_total_enforcement
        self._protocol = protocol
        self._upstream_api = upstream_api
        self._anthropic_user_id_mode = anthropic_user_id_mode
        self._anthropic_session_id = anthropic_session_id
        self._tool_replay = None
        if anthropic_tool_replay_mode == "restore_leading_cd":
            from .anthropic_replay import ToolInputReplay
            self._tool_replay = ToolInputReplay(anthropic_tool_replay_sources)
        self._stop_on_http_400 = stop_on_http_400
        self._max_request_bytes = max_request_bytes
        self._max_response_bytes = max_response_bytes
        self._max_attempts = max_attempts
        self._rate_limit_recovery_seconds = float(rate_limit_recovery_seconds)
        self._sleep = sleep
        self._token = secrets.token_urlsafe(32)
        self._started_at: float | None = None
        self._tokens_used = 0
        self._tokens_reported = 0
        self._component_totals = dict.fromkeys(
            ("input_tokens", "cached_input_tokens", "uncached_input_tokens", "output_tokens"), 0)
        self._component_unknown = dict.fromkeys(self._component_totals, 0)
        self._unknown_usage_requests = 0
        self._last_settled_request_tokens: int | None = None
        self._request_in_flight = False
        self._usage_unknown = False
        self._finalization_warning_injected = False
        self._revoked = False
        self._stop_reason = None
        self._state_lock = threading.RLock()
        self._call_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._server = None
        self._thread = None

    @property
    def token(self) -> str:
        return self._token

    def restore_usage(self, checkpoint):
        """Restore cumulative accounting before opening a new model lease."""
        with self._state_lock:
            if self._started_at is not None:
                raise RuntimeError('usage can only be restored before gateway startup')
            def count(value):
                if type(value) is not int or value < 0:
                    raise ValueError('invalid usage checkpoint counter')
                return value
            used = count(checkpoint['tokens_used'])
            reported = count(checkpoint['tokens_reported'])
            unknown = count(checkpoint['unknown_usage_requests'])
            totals = {k: count(checkpoint['token_breakdown'][k]) for k in self._component_totals}
            missing = {k: count(checkpoint['token_breakdown_unknown_requests'][k]) for k in self._component_totals}
            if used < reported or used >= self._token_limit:
                raise ValueError('invalid or exhausted original token budget')
            if unknown and self._strict:
                raise ValueError('unknown historical usage prevents bounded resume')
            warning_injected = checkpoint.get('finalization_warning_injected', False)
            if type(warning_injected) is not bool:
                raise ValueError('invalid finalization warning checkpoint')
            last_settled = checkpoint.get('last_settled_request_tokens')
            if last_settled is not None and (type(last_settled) is not int
                                              or last_settled <= 0):
                raise ValueError('invalid last settled request checkpoint')
            self._tokens_used, self._tokens_reported = used, reported
            self._unknown_usage_requests = unknown
            self._last_settled_request_tokens = last_settled
            self._usage_unknown = bool(unknown)
            self._finalization_warning_injected = warning_injected
            self._component_totals, self._component_unknown = totals, missing

    @property
    def model(self) -> str:
        return self._model

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("gateway is not running")
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        suffix = "/v1" if self._protocol == "responses" else ""
        return f"http://127.0.0.1:{self.port}{suffix}"

    def __enter__(self):
        if self._server is not None:
            raise RuntimeError("gateway is already running")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802
                gateway._handle(self)

            def do_CONNECT(self):  # noqa: N802
                gateway._send(self, 405, _json_error("method_not_allowed", "CONNECT is disabled"))

            def log_message(self, _format, *_args):
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", self._requested_port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True,
                                        name="model-gateway")
        self._thread.start()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def _stop_locked(self, reason):
        # Preserve the actual cause when normal cleanup later revokes the lease.
        if self._stop_reason is None:
            self._stop_reason = reason
        self._revoked = True

    def _budget_message_locked(self):
        limit = self._token_limit if self._token_limit != float('inf') else 'unlimited'
        known = (f"Confirmed at least {self._tokens_reported} / {limit} tokens; "
                 f"{self._unknown_usage_requests} request(s) have unknown usage; actual remaining budget is unknown"
                 if self._usage_unknown else
                 f"Total usage: {self._tokens_reported} / {limit} tokens")
        known += "; confirmed breakdown: " + ", ".join(
            f"{name}={count}" + (f" (incomplete: {self._component_unknown[name]} requests)"
                                 if self._component_unknown[name] else "")
            for name, count in self._component_totals.items())
        messages = {
            "token_budget_exhausted": "This run's token limit has been reached; returned responses are preserved; further model requests are stopped",
            # Retained so historical evidence with this stop reason still renders clearly.
            "usage_unknown_limit": "Multiple responses in this run have missing usage; accounting stopped under the former policy",
            "usage_unknown": "Strict accounting encountered unknown usage; further model requests are stopped",
            "token_bound_exceeded": "Provider-reported usage exceeded the reserved bound; strict accounting has stopped",
            "deadline_exhausted": f"This run's time limit has been reached (model gateway deadline: {self._deadline_seconds:g} seconds)",
            "submission_selected": "The final program has been selected; model access has ended",
            "session_closed": "This run's session has been closed; model access has ended",
        }
        if self._stop_reason:
            return f"{self._stop_reason}: {messages.get(self._stop_reason, 'Model access has stopped')}. {known}."
        if self._usage_unknown:
            if self._token_limit == float('inf') and not self._strict:
                return (f"Usage warning: {known}. No token limit is configured; missing usage does not stop model access. "
                        "Recorded usage is incomplete and is only a lower bound; exact total cost is unknown. "
                        "The deadline and other stop conditions still apply. The client is responsible for retries.")
            return (f"Usage warning: {known}. Model access remains available and ordinary client recovery may continue. "
                    "The confirmed-token threshold and deadline still apply, but unknown requests may have been billed and can make actual usage exceed the configured limit. "
                    "The client is responsible for retries.")
        return known + ". Usage is settled after each API response; in-flight requests are not yet accounted for."

    def _finalization_warning_threshold_locked(self) -> int | None:
        if self._token_limit == float('inf'):
            return None
        return ((self._token_limit * _FINALIZATION_WARNING_NUMERATOR
                 + _FINALIZATION_WARNING_DENOMINATOR - 1)
                // _FINALIZATION_WARNING_DENOMINATOR)

    def _finalization_warning_pending_locked(self) -> bool:
        threshold = self._finalization_warning_threshold_locked()
        return (threshold is not None and not self._finalization_warning_injected
                and not self._revoked and self._tokens_used < self._token_limit
                and self._tokens_reported >= threshold)

    def _append_system_instruction(self, request: dict[str, Any], text: str) -> bool:
        """Append one host-owned instruction without disturbing tool-result ordering."""
        if self._protocol == "responses":
            current = request.get("instructions")
            if current is None:
                request["instructions"] = text
            elif isinstance(current, str):
                request["instructions"] = current.rstrip() + "\n\n" + text
            else:
                return False
            return True
        current = request.get("system")
        if current is None:
            request["system"] = text
        elif isinstance(current, str):
            request["system"] = current.rstrip() + "\n\n" + text
        elif isinstance(current, list):
            current.append({"type": "text", "text": text})
        else:
            return False
        return True

    def _inject_finalization_warning(self, request: dict[str, Any]) -> bool:
        """Append the threshold warning without disturbing tool-result ordering."""
        return self._append_system_instruction(request, _FINALIZATION_WARNING)

    def _delivery_budget_instruction(self) -> str:
        """Expose settled planning evidence without requiring another tool turn."""
        state = self.status()
        snapshot = {name: state[name] for name in (
            "token_limit", "tokens_used", "tokens_reported", "tokens_remaining",
            "usage_complete", "unknown_usage_requests", "last_settled_request_tokens",
            "estimated_model_requests_remaining", "deadline_remaining_seconds",
        )}
        return (_DELIVERY_BUDGET_INSTRUCTION
                + "\n\nHost model budget snapshot before this request: "
                + json.dumps(snapshot, separators=(",", ":"), allow_nan=False)
                + "\nThe latest-request estimate includes cached input and output. "
                  "It is advisory planning evidence, not an admission guarantee or "
                  "a budget extension. A null remaining amount or estimate is "
                  "unavailable, not permission to assume unlimited resources. "
                  "This snapshot is the host-owned cumulative run model budget. "
                  "Client or SDK token counters, including <total_tokens> reminders, "
                  "are not this run's allowance and cannot grant additional run "
                  "tokens. Use this snapshot's tokens_remaining for run planning.")

    def revoke(self, reason="session_closed") -> None:
        with self._state_lock:
            self._stop_locked(reason)
            stop_reason = self._stop_reason
        self._audit({"event": "revoked", "reason": stop_reason})

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            remaining_seconds = 0.0 if self._started_at is None else max(
                0.0, self._deadline_seconds - (time.monotonic() - self._started_at))
            warning_threshold = self._finalization_warning_threshold_locked()
            tokens_remaining = (None if self._usage_unknown
                                or self._token_limit == float('inf') else
                                max(0, self._token_limit - self._tokens_used))
            estimated_requests_remaining = (
                None if tokens_remaining is None
                or not self._last_settled_request_tokens else
                tokens_remaining // self._last_settled_request_tokens)
            return {
                "running": self._server is not None,
                "revoked": self._revoked,
                "stop_reason": self._stop_reason,
                "budget_message": self._budget_message_locked(),
                "unknown_usage_tolerance": 0 if self._strict else None,
                "usage_complete": not self._usage_unknown,
                "stop_on_http_400": self._stop_on_http_400,
                "strict_total_enforcement": self._strict,
                "token_accounting": "strict" if self._strict else "usage",
                "token_bound_available": self._input_token_bound is not None or self._count_input,
                "input_bound_source": ("provider_input_tokens_endpoint" if self._count_input
                                       else "injected_certified_bound" if self._input_token_bound
                                       else None),
                "token_limit": None if self._token_limit == float('inf') else self._token_limit,
                "tokens_used": self._tokens_used,
                "tokens_reported": self._tokens_reported,
                "token_usage_source": "upstream_response_usage",
                "token_breakdown": dict(self._component_totals),
                "token_breakdown_unknown_requests": dict(self._component_unknown),
                "tokens_remaining": tokens_remaining,
                "tokens_overshoot": max(0, self._tokens_reported - self._token_limit),
                "last_settled_request_tokens": self._last_settled_request_tokens,
                "estimated_model_requests_remaining": estimated_requests_remaining,
                "model_request_estimate_basis": (None if self._last_settled_request_tokens is None
                                                 else "latest_settled_response_usage"),
                "finalization_warning_fraction": (None if warning_threshold is None else
                                                  _FINALIZATION_WARNING_NUMERATOR /
                                                  _FINALIZATION_WARNING_DENOMINATOR),
                "finalization_warning_threshold_tokens": warning_threshold,
                "finalization_warning_pending": self._finalization_warning_pending_locked(),
                "finalization_warning_injected": self._finalization_warning_injected,
                "request_in_flight": self._request_in_flight,
                "unknown_usage_requests": self._unknown_usage_requests,
                "usage_unknown": self._usage_unknown,
                "deadline_remaining_seconds": None if self._deadline_seconds == float('inf') else remaining_seconds,
                "request_timeout_seconds": _REQUEST_TIMEOUT_SECONDS,
                "rate_limit_recovery_seconds": self._rate_limit_recovery_seconds,
                "request_byte_limit": self._max_request_bytes,
                "response_byte_limit": self._max_response_bytes,
                "default_max_output_tokens": self._default_max_output_tokens,
                "protocol": self._protocol,
                "upstream_api": self._upstream_api,
            }

    def _audit(self, record: Mapping[str, Any]) -> None:
        row = {"ts": time.time(), **record}
        with self._log_lock:
            with self._log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _send(handler, status: int, body: bytes, content_type="application/json") -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()

    def _reject(self, handler, status: int, code: str, message: str) -> None:
        self._audit({"event": "rejected", "code": code})
        if self._protocol == "anthropic":
            body = json.dumps({"type":"error","error":{"type":code,"message":message}},
                              separators=(",", ":")).encode("utf-8")
        else:
            body = _json_error(code, message)
        self._send(handler, status, body)

    def _handle(self, handler) -> None:
        path = urlsplit(handler.path).path
        routes = _ROUTES if self._protocol == "responses" else _ANTHROPIC_ROUTES
        if path not in routes:
            self._reject(handler, 404, "route_not_found",
                         f"only {self._protocol} model routes are exposed")
            return
        authorization = handler.headers.get("Authorization", "")
        supplied = authorization.removeprefix("Bearer ") or handler.headers.get("x-api-key", "")
        if not hmac.compare_digest(supplied, self._token):
            self._reject(handler, 401, "unauthorized", "invalid gateway credential")
            return
        try:
            length = int(handler.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self._reject(handler, 411, "content_length_required", "Content-Length is required")
            return
        if length > self._max_request_bytes:
            self._reject(handler, 413, "request_too_large", "request exceeds byte limit")
            return
        raw = handler.rfile.read(length)
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reject(handler, 400, "invalid_json", "request body must be JSON")
            return
        if not isinstance(request, dict):
            self._reject(handler, 400, "invalid_request", "request body must be an object")
            return
        if _remote_reference(request):
            self._reject(handler, 400, "remote_reference_forbidden",
                         "remote URL and provider-side file references are disabled")
            return
        request["model"] = self._model
        if (self._protocol == "anthropic" and path == "/v1/messages"
                and not self._model.lower().startswith("claude")):
            thinking = request.get("thinking")
            if isinstance(thinking, dict) and thinking.get("type") == "adaptive":
                # Claude Code uses Anthropic's native ``adaptive`` value. Some
                # Anthropic-compatible OpenAI routes expose the same field but
                # accept only enabled/disabled/auto; ``auto`` preserves the
                # caller's request to let the model choose its reasoning mode.
                thinking["type"] = "auto"
        if path == "/v1/responses" and self._model.lower().startswith("glm-"):
            reasoning = request.get("reasoning")
            if isinstance(reasoning, dict):
                # The GLM OpenAI-compatible Responses route accepts reasoning
                # effort but rejects Codex's optional summary control.
                reasoning.pop("summary", None)
            tools = request.get("tools")
            if isinstance(tools, list):
                # This route also rejects OpenAI-hosted built-ins such as
                # web_search. Preserve ordinary function and namespace tools,
                # including the benchmark's game MCP surface.
                request["tools"] = [tool for tool in tools
                                    if not (isinstance(tool, dict)
                                            and tool.get("type") == "web_search")]
        if path == "/v1/responses" and request.get("max_output_tokens") is None:
            request["max_output_tokens"] = self._default_max_output_tokens
        if self._protocol == "anthropic" and path == "/v1/messages" and request.get("max_tokens") is None:
            request["max_tokens"] = self._default_max_output_tokens
        normalized = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(normalized) > self._max_request_bytes:
            self._reject(handler, 413, "request_too_large", "normalized request exceeds byte limit")
            return
        if path == "/v1/messages/count_tokens":
            self._passthrough(handler, path, normalized)
        else:
            forwarded_headers = {}
            if self._protocol == "anthropic":
                for name in ("anthropic-beta", "anthropic-version"):
                    value = handler.headers.get(name)
                    if value:
                        forwarded_headers[name] = value
            self._forward_serialized(handler, path, request, normalized,
                                     client_headers=forwarded_headers)

    def _upstream_request(self, path, client_headers=None):
        if self._protocol == "responses":
            url = (self._upstream + "/chat/completions" if self._upstream_api == "chat"
                   else self._upstream + path.removeprefix("/v1"))
            headers = {"Authorization": f"Bearer {self._api_key}"}
        else:
            url = (self._upstream + path.removeprefix("/v1")
                   if self._upstream.endswith("/v1") else self._upstream + path)
            headers = {"Authorization": f"Bearer {self._api_key}",
                       "x-api-key": self._api_key, "anthropic-version": "2023-06-01"}
        headers.update(dict(client_headers or {}))
        headers.update({"Content-Type": "application/json",
                        "Accept": "text/event-stream, application/json"})
        return url, headers

    def _passthrough(self, handler, path, body):
        """Forward provider token-count calls without charging generation usage."""
        request_id = secrets.token_hex(12)
        with self._state_lock:
            if self._revoked:
                self._reject(handler, 403, self._stop_reason or "session_closed",
                             self._budget_message_locked())
                return
            if time.monotonic() - self._started_at >= self._deadline_seconds:
                self._stop_locked("deadline_exhausted")
                self._reject(handler, 403, "deadline_exhausted", self._budget_message_locked())
                return
        url, headers = self._upstream_request(path)
        response = None
        try:
            timeout = min(_REQUEST_TIMEOUT_SECONDS,
                          self._deadline_seconds - (time.monotonic() - self._started_at))
            if timeout <= 0:
                self._reject(handler, 403, "deadline_exhausted", "gateway deadline expired")
                return
            response = self._transport("POST", url, headers, body, timeout)
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > self._max_response_bytes:
                    self._reject(handler, 502, "response_too_large", "upstream response exceeds byte limit")
                    return
            self._audit({"event": "unmetered_count", "request_id": request_id,
                         "route": path, "upstream_status": response.status})
            content_type = next((v for k, v in response.headers.items()
                                 if k.lower() == "content-type"), "application/json")
            self._send(handler, response.status, bytes(data), content_type)
        except Exception as error:
            self._audit({"event": "unmetered_count_error", "request_id": request_id,
                         "error_type": type(error).__name__})
            self._reject(handler, 502, "upstream_unavailable", "upstream count request failed")
        finally:
            if response is not None:
                response.close()

    def _provider_input_count(self, request: Mapping[str, Any], request_id: str) -> int:
        # These are exactly the request fields documented for
        # POST /responses/input_tokens.  In particular generation-only fields
        # such as max_output_tokens and stream are intentionally excluded.
        if self._protocol == "responses":
            count_fields = {
                "conversation", "input", "instructions", "model", "parallel_tool_calls",
                "personality", "previous_response_id", "reasoning", "text", "tool_choice",
                "tools", "truncation",
            }
            count_path = "/v1/responses/input_tokens"
        else:
            count_fields = {"model", "messages", "system", "tools", "tool_choice"}
            count_path = "/v1/messages/count_tokens"
        count_request = {key: value for key, value in request.items() if key in count_fields}
        body = json.dumps(count_request, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        url, headers = self._upstream_request(count_path)
        headers["Accept"] = "application/json"
        reason, http_status = "transport_error", None
        for attempt in range(1, self._max_attempts + 1):
            timeout = min(120, self._deadline_seconds - (time.monotonic() - self._started_at))
            if timeout <= 0:
                raise ValueError("gateway deadline expired during input token count")
            response = None
            reason, http_status = "transport_error", None
            try:
                response = self._transport("POST", url, headers, body, timeout)
                http_status = response.status if type(response.status) is int else None
                reason = ("endpoint_unsupported" if http_status in {404, 405, 501}
                          else "http_error")
                if http_status != 200:
                    raise _InputTokenCountError(reason, http_status)
                reason = "invalid_response"
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 64 * 1024:
                        reason = "response_too_large"
                        raise ValueError("input token count response exceeds 64 KiB")
                value = json.loads(payload)
                count = value.get("input_tokens") if isinstance(value, dict) else None
                if response.status != 200 or not isinstance(count, int) or isinstance(count, bool) or count < 0:
                    raise ValueError("provider did not return a valid input token count")
                self._audit({"event": "input_token_count_attempt", "request_id": request_id,
                             "attempt": attempt, "status": "success", "input_tokens": count,
                             "http_status": http_status,
                             "count_request": count_request})
                return count
            except Exception as error:
                self._audit({"event": "input_token_count_attempt", "request_id": request_id,
                             "attempt": attempt, "status": "error",
                             "http_status": http_status, "reason": reason,
                             "error_type": type(error).__name__})
            finally:
                if response is not None:
                    response.close()
            if reason == "endpoint_unsupported":
                break
        raise _InputTokenCountError(reason, http_status)

    def _reservation(self, path: str, request: Mapping[str, Any],
                     request_id: str) -> tuple[int | None, str | None]:
        if path == "/v1/responses/compact":
            if self._strict:
                return None, ("the documented compact endpoint has no enforceable output-token cap; "
                              "strict total-token mode disables it")
            return 0, None  # Usage-only: compact has no prospective reservation.
        output_key = "max_output_tokens" if self._protocol == "responses" else "max_tokens"
        output = request.get(output_key)
        if not isinstance(output, int) or isinstance(output, bool) or output <= 0:
            return None, "a positive max_output_tokens/output reserve is required"
        if self._strict:
            if self._input_token_bound is None and not self._count_input:
                return None, "no provider-certified input token bound is configured"
            try:
                input_bound = (self._provider_input_count(request, request_id)
                               if self._count_input else self._input_token_bound(request))
            except _InputTokenCountError as error:
                return None, str(error)
            except Exception:
                return None, "provider-certified input token bound failed"
            if not isinstance(input_bound, int) or isinstance(input_bound, bool) or input_bound < 0:
                return None, "provider-certified input token bound is invalid"
            return input_bound + output, None
        return 0, None  # Output limits are not input+output accounting bounds.

    def _normalize_anthropic_user_id(self, request):
        """Normalize only an explicitly authorized original session identifier."""
        if self._anthropic_user_id_mode == "preserve":
            return None
        metadata = request.get("metadata")
        if not isinstance(metadata, dict) or "user_id" not in metadata:
            return None
        original = metadata["user_id"]
        if original == self._anthropic_session_id:
            return None
        try:
            structured = json.loads(original) if isinstance(original, str) else None
        except ValueError:
            structured = None
        if not isinstance(structured, dict) or structured.get("session_id") != self._anthropic_session_id:
            raise ValueError("Metadata user_id does not identify the authorized original CLI session")
        metadata["user_id"] = self._anthropic_session_id
        return {"transformation": "anthropic_metadata_user_id_to_session_uuid",
                "original_user_id_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
                "session_id": self._anthropic_session_id}

    def _forward_serialized(self, handler, path, request, normalized, client_headers=None) -> None:
        with self._call_lock:
            request_id = secrets.token_hex(12)
            warning_included = False
            warning_tokens_reported = None
            with self._state_lock:
                if self._revoked:
                    self._reject(handler, 403, self._stop_reason or "session_closed",
                                 self._budget_message_locked())
                    return
            try:
                compatibility = self._normalize_anthropic_user_id(request)
            except ValueError as error:
                self._reject(handler, 400, "metadata_session_mismatch", str(error))
                return
            replay_changes = []
            if self._tool_replay is not None:
                try:
                    replay_changes = self._tool_replay.normalize(request)
                except ValueError as error:
                    if self._stop_on_http_400:
                        self.revoke("tool_replay_mismatch")
                    self._reject(handler, 400, "tool_replay_mismatch", str(error))
                    return
            if compatibility is not None:
                self._audit({"event": "request_compatibility_transform", "request_id": request_id,
                             **compatibility})
            for change in replay_changes:
                self._audit({"event": "request_compatibility_transform", "request_id": request_id,
                             **change})
            delivery_instruction_included = path != "/v1/responses/compact"
            if delivery_instruction_included and not self._append_system_instruction(
                    request, self._delivery_budget_instruction()):
                self._reject(handler, 400, "invalid_system_instruction",
                             "cannot append the required host delivery and budget instruction")
                return
            if (delivery_instruction_included and self._protocol == "responses"
                    and self._model.lower().startswith("glm-")
                    and not self._append_system_instruction(
                        request, _GLM_CODEX_TOOL_COMPATIBILITY_INSTRUCTION)):
                self._reject(handler, 400, "invalid_system_instruction",
                             "cannot append the required GLM tool compatibility instruction")
                return
            with self._state_lock:
                if self._finalization_warning_pending_locked():
                    warning_included = self._inject_finalization_warning(request)
            if delivery_instruction_included or warning_included or compatibility is not None or replay_changes:
                normalized = json.dumps(
                    request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(normalized) > self._max_request_bytes:
                    self._reject(handler, 413, "request_too_large",
                                 "host budget instruction would exceed request byte limit")
                    return
            self._audit({"event": "request", "request_id": request_id, "route": path,
                         "request": request})
            with self._state_lock:
                if self._revoked:
                    self._reject(handler, 403, self._stop_reason or "session_closed",
                                 self._budget_message_locked())
                    return
                remaining_time = self._deadline_seconds - (time.monotonic() - self._started_at)
                if remaining_time <= 0:
                    self._stop_locked("deadline_exhausted")
                    self._reject(handler, 403, "deadline_exhausted", self._budget_message_locked())
                    return
                reservation, problem = self._reservation(path, request, request_id)
                if problem:
                    self._reject(handler, 503, "budget_bound_unavailable", problem)
                    return
                if self._tokens_used >= self._token_limit or (
                        self._strict and reservation > self._token_limit - self._tokens_used):
                    self._reject(handler, 429, "token_budget_exhausted",
                                 f"token_budget_exhausted: This request's reservation of {reservation} tokens exceeds the remaining budget; "
                                 f"accounted usage: {self._tokens_used} / {self._token_limit} tokens.")
                    return
                if warning_included:
                    self._finalization_warning_injected = True
                    warning_threshold = self._finalization_warning_threshold_locked()
                    warning_tokens_reported = self._tokens_reported
            self._audit({"event": "reservation", "request_id": request_id,
                         "token_reservation": reservation if self._strict else None,
                         "token_accounting": "strict" if self._strict else "usage"})
            if warning_included:
                self._audit({"event": "finalization_warning_injected",
                             "request_id": request_id,
                             "threshold_fraction": (_FINALIZATION_WARNING_NUMERATOR /
                                                    _FINALIZATION_WARNING_DENOMINATOR),
                             "threshold_tokens": warning_threshold,
                             "tokens_reported": warning_tokens_reported})
            url, headers = self._upstream_request(path, client_headers)
            response = None
            attempt = 0
            before_send_failures = 0
            rate_limit_retries = 0
            rate_limit_waited = 0.0
            with self._state_lock:
                self._request_in_flight = True
            while True:
                timeout = min(_REQUEST_TIMEOUT_SECONDS,
                              self._deadline_seconds - (time.monotonic() - self._started_at))
                if timeout <= 0:
                    break
                attempt += 1
                try:
                    response = self._transport("POST", url, headers, normalized, timeout)
                    self._audit({"event": "attempt", "request_id": request_id,
                                 "attempt": attempt, "status": "connected",
                                 "upstream_status": response.status})
                    if response.status == 400 and self._stop_on_http_400:
                        self.revoke("upstream_http_400")
                        self._audit({"event": "nonretryable_upstream_status",
                                     "request_id": request_id, "upstream_status": 400})
                    before_send_failures = 0
                    if response.status != 429:
                        break
                    rate_limit_retries += 1
                    retry_after = _retry_after_seconds(response.headers)
                    source = "retry_after" if retry_after is not None else "exponential"
                    requested_delay = (max(1.0, retry_after) if retry_after is not None else
                                       _RATE_LIMIT_INITIAL_BACKOFF_SECONDS *
                                       (2 ** min(rate_limit_retries - 1, 20)))
                    recovery_remaining = max(
                        0.0, self._rate_limit_recovery_seconds - rate_limit_waited)
                    deadline_remaining = self._deadline_seconds - (
                        time.monotonic() - self._started_at)
                    if retry_after is not None and requested_delay > recovery_remaining:
                        delay = None
                        reason = "retry_after_exceeds_recovery_window"
                    else:
                        delay = min(requested_delay, recovery_remaining)
                        reason = ("recovery_window_exhausted" if delay <= 0 else
                                  "deadline_exhausted" if delay >= deadline_remaining else None)
                    if reason is not None:
                        self._audit({"event": "rate_limit_exhausted",
                                     "request_id": request_id, "attempt": attempt,
                                     "reason": reason,
                                     "waited_seconds": rate_limit_waited,
                                     "recovery_seconds": self._rate_limit_recovery_seconds})
                        break
                    self._audit({"event": "rate_limit_backoff", "request_id": request_id,
                                 "attempt": attempt, "delay_seconds": delay,
                                 "source": source,
                                 "waited_seconds": rate_limit_waited,
                                 "recovery_seconds": self._rate_limit_recovery_seconds})
                    try:
                        response.close()
                    except Exception as error:
                        self._audit({"event": "response_cleanup_error",
                                     "request_id": request_id,
                                     "error_type": type(error).__name__})
                    response = None
                    self._sleep(delay)
                    rate_limit_waited += delay
                    continue
                except RetryableBeforeSendError as error:
                    self._audit({"event": "attempt", "request_id": request_id,
                                 "attempt": attempt, "status": "error",
                                 "error_type": type(error).__name__,
                                 "provider_acceptance": "not_sent"})
                    before_send_failures += 1
                    if before_send_failures < self._max_attempts:
                        continue
                    break
                except Exception as error:
                    self._audit({"event": "attempt", "request_id": request_id,
                                 "attempt": attempt, "status": "error",
                                 "error_type": type(error).__name__,
                                 "provider_acceptance": "unknown"})
                    self._retire_unknown(
                        reservation, request_id,
                        rearm_finalization_warning=warning_included)
                    self._reject(handler, 502, "upstream_usage_unknown",
                                 "upstream failure has unknown billing outcome")
                    return
            if response is None:
                # All attempts certified that nothing was sent (or the
                # deadline expired before sending). This is known zero use.
                with self._state_lock:
                    self._request_in_flight = False
                    if warning_included:
                        self._finalization_warning_injected = False
                if warning_included:
                    self._audit({"event": "finalization_warning_rearmed",
                                 "request_id": request_id,
                                 "reason": "request_not_sent"})
                self._audit({"event": "request_not_sent", "request_id": request_id})
                self._reject(handler, 502, "upstream_unavailable", "upstream request failed")
                return
            self._relay(handler, response, reservation, request_id)

    def _retire_unknown(self, reservation: int, request_id: str, *,
                        rearm_finalization_warning: bool = False) -> None:
        with self._state_lock:
            if self._strict:
                self._tokens_used += reservation
            self._unknown_usage_requests += 1
            for name in self._component_unknown:
                self._component_unknown[name] += 1
            self._request_in_flight = False
            self._usage_unknown = True
            if rearm_finalization_warning:
                # Ambiguous provider acceptance does not prove the client
                # received a response influenced by the injected warning.
                self._finalization_warning_injected = False
            if time.monotonic() - self._started_at >= self._deadline_seconds:
                self._stop_locked("deadline_exhausted")
            elif self._strict:
                self._stop_locked("usage_unknown")
            revoked, reason = self._revoked, self._stop_reason
        if rearm_finalization_warning:
            self._audit({"event": "finalization_warning_rearmed",
                         "request_id": request_id,
                         "reason": "upstream_outcome_unknown"})
        self._audit({"event": "usage", "request_id": request_id, "usage": "unknown",
                     "charged_tokens": reservation if self._strict else None,
                     "token_usage_source": "upstream_response_usage",
                     "token_breakdown": dict.fromkeys(self._component_totals),
                     "gateway_revoked": revoked, "stop_reason": reason})

    def _relay(self, handler, response, reservation: int, request_id: str) -> None:
        content_type = next((v for k, v in response.headers.items()
                             if k.lower() == "content-type"), "application/octet-stream")
        streaming = content_type.lower().startswith("text/event-stream")
        total = 0
        usage = None
        buffered = bytearray()
        sse_pending = bytearray()
        settled = False
        anthropic_usage = {}
        components = {}
        collector = None
        if self._tool_replay is not None and response.status == 200:
            from .anthropic_replay import ToolUseCollector
            collector = ToolUseCollector(self._model)

        def observed_usage(value):
            nonlocal components
            if collector is not None:
                collector.observe(value)
            if self._protocol == "anthropic" and isinstance(value, Mapping):
                current = value.get("usage")
                if not isinstance(current, Mapping) and isinstance(value.get("message"), Mapping):
                    current = value["message"].get("usage")
                if isinstance(current, Mapping):
                    for name in ("input_tokens", "output_tokens",
                                 "cache_creation_input_tokens", "cache_read_input_tokens"):
                        number = current.get(name)
                        if type(number) is int and number >= 0:
                            anthropic_usage[name] = number
                if "input_tokens" in anthropic_usage and "output_tokens" in anthropic_usage:
                    components = _usage_components({"usage": anthropic_usage}, "anthropic")
                    return sum(anthropic_usage.values())
                return None
            result = _usage_from_json(value)
            if result is not None:
                components = _usage_components(value, self._protocol)
            return result

        def remember_inputs():
            if collector is None:
                return
            for call in collector.calls():
                try:
                    self._tool_replay.remember(call, request_id)
                except ValueError:
                    self.revoke("tool_replay_source_conflict")
                    self._audit({"event": "tool_replay_source_conflict",
                                 "request_id": request_id, "tool_use_id": call["id"]})
                    return
                self._audit({"event": "tool_replay_source", "request_id": request_id,
                             "tool_use_id": call["id"]})

        def settle(known_usage=None):
            # One request owns one charge, even if delivery/auditing/cleanup
            # raises after settlement. Set the guard before auditing, whose
            # failure must not trigger a second conservative charge.
            nonlocal settled
            if settled:
                return
            settled = True
            if known_usage is None:
                self._retire_unknown(reservation, request_id)
                return
            with self._state_lock:
                self._tokens_used += known_usage
                self._tokens_reported += known_usage
                if known_usage > 0:
                    self._last_settled_request_tokens = known_usage
                for name in self._component_totals:
                    if name in components:
                        self._component_totals[name] += components[name]
                    else:
                        self._component_unknown[name] += 1
                self._request_in_flight = False
                if self._strict and known_usage > reservation:
                    self._stop_locked("token_bound_exceeded")
                elif self._tokens_used >= self._token_limit:
                    self._stop_locked("token_budget_exhausted")
                revoked = self._revoked
                stop_reason = self._stop_reason
            self._audit({"event": "usage", "request_id": request_id,
                         "usage": known_usage, "charged_tokens": known_usage,
                         "token_usage_source": "upstream_response_usage",
                         "token_breakdown": {name: components.get(name) for name in self._component_totals},
                         "gateway_revoked": revoked, "stop_reason": stop_reason})

        try:
            if streaming:
                handler.send_response(response.status)
                handler.send_header("Content-Type", content_type)
                handler.send_header("Connection", "close")
                handler.end_headers()
            for chunk in response.iter_bytes():
                if time.monotonic() - self._started_at >= self._deadline_seconds:
                    self._audit({"event": "response_interrupted", "request_id": request_id,
                                 "error_type": "GatewayDeadlineExceeded"})
                    settle()
                    return
                total += len(chunk)
                if total > self._max_response_bytes:
                    self._audit({"event": "response_rejected", "request_id": request_id,
                                 "code": "response_too_large", "bytes_seen": total})
                    settle()
                    if not streaming:
                        self._send(handler, 502, _json_error("response_too_large",
                                                             "upstream response exceeds byte limit"))
                    return
                self._audit({"event": "response_chunk", "request_id": request_id,
                             "data_hex": chunk.hex()})
                if streaming:
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    sse_pending.extend(chunk)
                    # Accept LF and CRLF SSE framing, including split chunks.
                    while b"\n\n" in sse_pending or b"\r\n\r\n" in sse_pending:
                        separators = [sep for sep in (b"\n\n", b"\r\n\r\n") if sep in sse_pending]
                        sep = min(separators, key=sse_pending.index)
                        event, _, rest = sse_pending.partition(sep)
                        sse_pending = bytearray(rest)
                        for line in event.splitlines():
                            if line.startswith(b"data:"):
                                try:
                                    value = json.loads(line[5:].strip())
                                    if self._protocol == "responses" and value.get("type") not in {
                                            "response.completed", "response.incomplete"}:
                                        found = None
                                    else:
                                        found = observed_usage(value)
                                except (json.JSONDecodeError, UnicodeDecodeError):
                                    found = None
                                if found is not None:
                                    usage = found
                else:
                    buffered.extend(chunk)
            if not streaming:
                try:
                    usage = observed_usage(json.loads(buffered))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    usage = None
                # A Content-Length client can finish reading before the
                # handler returns. Publish the charge before its response.
                settle(usage)
                remember_inputs()
                self._send(handler, response.status, bytes(buffered), content_type)
            else:
                # Keep incremental SSE delivery; settle after consuming the
                # stream, before closing upstream or exposing downstream EOF.
                settle(usage)
                remember_inputs()
        except Exception as error:
            self._audit({"event": "response_interrupted", "request_id": request_id,
                         "error_type": type(error).__name__})
        finally:
            try:
                # Interrupted streams have no trustworthy completed usage.
                # This runs even if the error audit itself failed.
                settle()
            finally:
                try:
                    response.close()
                except Exception as error:
                    # Cleanup must never erase a completed or unknown charge.
                    self._audit({"event": "response_cleanup_error",
                                 "request_id": request_id,
                                 "error_type": type(error).__name__})


__all__ = ["ModelGateway", "RetryableBeforeSendError"]
