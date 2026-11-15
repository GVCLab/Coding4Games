"""Host-only operator settings and provider routes."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from coding4game_gui.paths import REPO_ROOT

PATH_DEFAULTS = {
    "profile": "runtime/profile.json",
    "credentials": "litellm.config.yaml",
    "output_root": "out/experiments",
}


def operator_paths(repo: Path = REPO_ROOT) -> dict[str, Path]:
    """Resolve paths relative to the checkout, independent of the caller's cwd."""
    repo = Path(repo).resolve()
    preset = repo / "launcher.local.json"
    values = {}
    if preset.is_file():
        try:
            values = json.loads(preset.read_text(encoding="utf-8-sig"))
            if not isinstance(values, dict):
                raise ValueError()
        except (OSError, ValueError):
            raise ValueError("Cannot read launcher.local.json; check its JSON structure.") from None
    resolved = {}
    for name, default in PATH_DEFAULTS.items():
        value = values.get(name, default)
        if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
            raise ValueError(f"Launcher {name} must be a nonempty path.")
        path = Path(value).expanduser()
        resolved[name] = (repo / path).resolve()
    return resolved


def initialize(repo: Path = REPO_ROOT) -> list[str]:
    """Create missing local settings from the shipped examples."""
    repo = Path(repo)
    examples = repo / "docs" / "examples"
    created = []
    for source, target in (
        ("launcher.json", "launcher.local.json"),
        ("provider.yaml", "litellm.config.yaml"),
        ("games.json", "games.local.json"),
    ):
        data = (examples / source).read_bytes()
        try:
            with (repo / target).open("xb") as stream:
                stream.write(data)
        except FileExistsError:
            continue
        created.append(target)
    return created


LAUNCHER_DEFAULTS = dict(
    **PATH_DEFAULTS, model="my-model", windows_input="auto",
    tic_budget=1200, wall_seconds=300, token_limit=30000,
)


@dataclass(frozen=True)
class ProviderRoute:
    model: str
    api_base: str
    api_key: str
    provider_model: str
    api_mode: str


def validate_preset(values):
    required = set(LAUNCHER_DEFAULTS) - {"output_root"}
    if not required <= set(values) <= set(LAUNCHER_DEFAULTS):
        raise ValueError("Launcher preset has missing or unsupported fields (no credentials allowed).")
    for name in ("profile", "model", "credentials"):
        if not isinstance(values[name], str) or not values[name].strip():
            raise ValueError(f"Set launcher {name} first.")
    if values["windows_input"] not in {"auto", "gamepad", "gamepad-kbm"}:
        raise ValueError("Invalid launcher input mode.")
    for name in ("tic_budget", "wall_seconds", "token_limit"):
        if type(values[name]) is not int or values[name] <= 0:
            raise ValueError(f"{name} must be a positive integer.")
    if "output_root" in values and (not isinstance(values["output_root"], str)
            or not values["output_root"].strip()):
        raise ValueError("output_root must be a nonempty path.")
    return dict(values)


def load_preset(path):
    if not Path(path).exists():
        return dict(LAUNCHER_DEFAULTS)
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return validate_preset(data)
    except (OSError, ValueError, TypeError):
        raise ValueError("Cannot read launcher preset; check its non-secret fields.") from None


def save_preset(path, values):
    data = validate_preset(values)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_path(repo, value):
    path = Path(value)
    return path if path.is_absolute() else Path(repo) / path


def load_provider_route(path, selector, *, allow_non_openai=False):
    """Resolve one configured route without exposing provider options or keys."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8-sig"))
        matches = [entry for entry in data["model_list"]
                   if entry.get("model_name") == selector or
                   entry.get("litellm_params", {}).get("model") == "openai/" + selector]
        if len(matches) != 1:
            raise ValueError()
        entry = matches[0]
        params = entry["litellm_params"]
        model = params["model"]
        base = params["api_base"]
        key = params["api_key"]
        if not isinstance(model, str) or not model.strip():
            raise ValueError()
        if not allow_non_openai and not model.startswith("openai/"):
            raise ValueError()
        upstream_model = model.split("/", 1)[-1]
        if (not upstream_model.strip() or "\x00" in upstream_model
                or "\n" in upstream_model or "\r" in upstream_model):
            raise ValueError()
        url = urlsplit(base)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        if not isinstance(key, str):
            raise ValueError()
        if key.startswith("os.environ/"):
            key = os.environ.get(key.removeprefix("os.environ/"), "")
        if not key.strip() or "\x00" in key or "\n" in key or "\r" in key:
            raise ValueError()
        model_info = entry.get("model_info") or {}
        if not isinstance(model_info, dict):
            raise ValueError()
        api_mode = model_info.get("mode", "responses")
        if api_mode not in {"responses", "chat"}:
            raise ValueError()
        if api_mode == "chat" and not model.startswith("openai/"):
            raise ValueError()
        return ProviderRoute(upstream_model, base, key, model, api_mode)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        protocol = "configured" if allow_non_openai else "openai"
        raise ValueError(f"Cannot load selected provider: require one matching {protocol} model, safe HTTPS api_base and a nonempty api_key (or os.environ/NAME).") from None
