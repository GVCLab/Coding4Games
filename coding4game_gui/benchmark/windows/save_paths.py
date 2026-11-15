"""Default save-state directories for the games we ship configs for.

Supplies known save locations for the checkpoint tool. Each entry is a list of
candidate paths (supports %ENV% / ~ / single glob); the first one that
exists on this machine wins.

If you add a game whose save dir isn't here, declare
`save_state.restore_to` in its YAML instead.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


KNOWN_SAVE_PATHS: dict[str, list[str]] = {
    "brotato": [
        # Saves live per-SteamID directly under Brotato/ (save_v3_0.json etc.,
        # no saves/ subdir); snapshot the whole dir so configs travel too.
        "%APPDATA%/Brotato",
    ],
}


def _expand(raw: str) -> Optional[Path]:
    """Resolve a path string (handling %ENV%, ~, glob). Returns None if no match exists."""
    expanded = os.path.expandvars(os.path.expanduser(raw))
    if "*" not in expanded and "?" not in expanded:
        p = Path(expanded)
        return p.resolve() if p.exists() else None
    p = Path(expanded)
    parts = p.parts
    for i, part in enumerate(parts):
        if "*" in part or "?" in part:
            base = Path(*parts[:i]) if i > 0 else Path(p.anchor or ".")
            rest = Path(*parts[i:])
            matches = sorted(base.glob(str(rest)))
            return matches[0].resolve() if matches else None
    return None


def default_save_path(game_name: str) -> Path:
    """Resolve a game's save dir from the registry. Raises if unknown / not present."""
    candidates = KNOWN_SAVE_PATHS.get(game_name)
    if not candidates:
        raise KeyError(
            f"no default save path known for game '{game_name}'. "
            f"Add an entry to KNOWN_SAVE_PATHS in coding4game_gui/benchmark/windows/save_paths.py, "
            f"or set save_state.restore_to in games/{game_name}.yaml."
        )
    for raw in candidates:
        resolved = _expand(raw)
        if resolved is not None:
            return resolved
    raise FileNotFoundError(
        f"none of the candidate save paths for '{game_name}' exist on this machine: "
        f"{candidates}. Either install the game first, or set save_state.restore_to "
        f"in games/{game_name}.yaml to point at where it actually stores saves."
    )
