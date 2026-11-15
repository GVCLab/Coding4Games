"""Apply explicit machine-local launch/save locations without changing gameplay."""
from __future__ import annotations

import copy
import json
from pathlib import Path


def read_driver_data(path: Path) -> dict:
    """Load a driver and its checkout's optional host-only path overrides."""
    import yaml

    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    local = path.parent.parent / "games.local.json"
    if not local.is_file() or path.parent.name != "games":
        return data
    try:
        overrides = json.loads(local.read_text(encoding="utf-8-sig"))
        if not isinstance(overrides, dict):
            raise ValueError()
        allowed = {"auto_launch": {"launch_path"}, "save_state": {"restore_to"}}
        for game, sections in overrides.items():
            if not isinstance(game, str) or not game or any(c in game for c in "/\\.:"):
                raise ValueError()
            if not (path.parent / f"{game}.yaml").is_file():
                raise ValueError()
            if not isinstance(sections, dict) or set(sections) - set(allowed):
                raise ValueError()
            for section, fields in sections.items():
                if not isinstance(fields, dict) or set(fields) - allowed[section]:
                    raise ValueError()
                for value in fields.values():
                    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\x00\r\n"):
                        raise ValueError()
    except (OSError, ValueError, TypeError):
        raise ValueError("Invalid games.local.json: only auto_launch.launch_path and save_state.restore_to are supported.") from None
    selected = overrides.get(path.stem, {})
    if not isinstance(data, dict):
        return data
    result = copy.deepcopy(data)
    for section, fields in selected.items():
        if not isinstance(result.get(section), dict):
            raise ValueError(f"Cannot override undeclared driver section: {section}.")
        result[section].update(fields)
    return result
