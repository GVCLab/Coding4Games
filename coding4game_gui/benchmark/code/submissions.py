"""Immutable, content-addressed executable programs developed in an isolated runtime."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from coding4game_gui.runtime_client.bundle import filename


FORMAT = "c4g-program-v1"
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_FILES = 16_384


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _json_object(value: Any, label: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain JSON values") from exc


def _entrypoint(value: Any, files: dict[str, bytes]) -> dict:
    if not isinstance(value, dict) or set(value) != {"argv", "mode", "runtime"}:
        raise ValueError("entrypoint requires exactly argv, mode, and runtime")
    argv = value["argv"]
    if (not isinstance(argv, list) or not argv
            or any(not isinstance(item, str) or not item or "\x00" in item
                   for item in argv)):
        raise ValueError("entrypoint argv must be a nonempty string list")
    first = filename(argv[0])
    if first not in files:
        raise ValueError("entrypoint must name a bundled file")
    validate_program_contract(value)
    if Path(first).suffix.casefold() != ".py":
        raise ValueError("python entrypoint must be a bundled .py file")
    return {"argv": [first, *argv[1:]], "mode": value["mode"], "runtime": value["runtime"]}


def validate_program_contract(entrypoint: dict) -> None:
    """Check executability independently of historical archive integrity."""
    if entrypoint.get("mode") != "stepped":
        raise ValueError("Program entrypoint mode must be stepped")
    if entrypoint.get("runtime") != "python":
        raise ValueError("WSL2 currently supports Python submitted programs only")


class ProgramStore:
    """Store immutable programs and an explicit final-program pointer."""

    def __init__(self, root: Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 max_files: int = DEFAULT_MAX_FILES):
        self.root = Path(root)
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if type(max_files) is not int or max_files <= 0:
            raise ValueError("max_files must be positive")
        self.max_bytes, self.max_files = max_bytes, max_files

    def _normalize_files(self, files: dict[str, bytes]) -> dict[str, bytes]:
        if not isinstance(files, dict) or not files or len(files) > self.max_files:
            raise ValueError(f"program must contain 1..{self.max_files} files")
        result, folded, total = {}, set(), 0
        for raw_name, data in files.items():
            name = filename(raw_name)
            key = name.casefold()
            if key in folded:
                raise ValueError("duplicate Windows filename")
            if not isinstance(data, bytes):
                raise ValueError("program file content must be bytes")
            folded.add(key)
            total += len(data)
            if total > self.max_bytes:
                raise ValueError(f"program exceeds configured {self.max_bytes}-byte limit")
            result[name] = data
        for key in folded:
            parts = key.split("/")
            if any("/".join(parts[:n]) in folded for n in range(1, len(parts))):
                raise ValueError("file/directory collision in program")
        return result

    @staticmethod
    def _declared_dependencies(files: dict[str, bytes], dependencies: Any) -> dict:
        bundled = None
        if "c4g-program.json" in files:
            try:
                metadata = json.loads(files["c4g-program.json"].decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid bundled c4g-program.json") from exc
            if not isinstance(metadata, dict):
                raise ValueError("bundled c4g-program.json must be an object")
            bundled = metadata.get("dependencies")
        if dependencies is not None and bundled is not None:
            explicit = _json_object(dependencies, "dependencies")
            if explicit != _json_object(bundled, "bundled dependencies"):
                raise ValueError("explicit and bundled dependency declarations differ")
            return explicit
        return _json_object(dependencies if dependencies is not None else bundled,
                            "dependencies")

    def save(self, files: dict[str, bytes], entrypoint: dict, description: str = "",
             dependencies: dict | None = None, observation_size=None,
             execution_platform: str | None = None) -> dict:
        files = self._normalize_files(files)
        entrypoint = _entrypoint(entrypoint, files)
        dependencies = self._declared_dependencies(files, dependencies)
        if not isinstance(description, str):
            raise ValueError("description must be a string")
        hashes = {name: hashlib.sha256(data).hexdigest()
                  for name, data in sorted(files.items())}
        identity = {"entrypoint": entrypoint, "dependencies": dependencies,
                    "files": hashes}
        if execution_platform not in (None, "windows", "linux"):
            raise ValueError("execution_platform must be windows or linux")
        if execution_platform is not None:
            identity["execution_platform"] = execution_platform
        if observation_size is not None:
            if list(observation_size) not in ([768, 461], [1000, 1000]):
                raise ValueError("unsupported program observation size")
            identity["observation_size"] = list(observation_size)
        program_id = hashlib.sha256(_canonical(identity)).hexdigest()
        target = self.root / program_id
        if target.exists():
            return self.get(program_id)[0]
        record = {
            "format": FORMAT, "id": program_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "description": description, "entrypoint": entrypoint,
            "dependencies": dependencies,
            "reproducibility": {
                "dependencies_declared": bool(dependencies),
                "dependencies_verified": False,
                "note": "Dependency declarations are metadata, not verification."},
            "files": hashes, "total_bytes": sum(map(len, files.values())),
        }
        if observation_size is not None:
            record["observation_size"] = list(observation_size)
        if execution_platform is not None:
            record["execution_platform"] = execution_platform
        record["record_sha256"] = hashlib.sha256(_canonical(record)).hexdigest()
        target.mkdir(parents=True, exist_ok=False)
        try:
            for name, data in files.items():
                path = target / "files" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            (target / "manifest.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        except BaseException:
            # Leave evidence of an incomplete archive; get() rejects it. Avoid a
            # recursive host deletion on paths derived from program contents.
            raise
        return record

    def get(self, program_id: str) -> tuple[dict, dict[str, bytes]]:
        if not isinstance(program_id, str) or not len(program_id) == 64 \
                or any(c not in "0123456789abcdef" for c in program_id):
            raise ValueError("invalid program id")
        target = self.root / program_id
        try:
            record = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise ValueError(f"program is missing or invalid: {program_id}") from exc
        if not isinstance(record, dict) or record.get("format") != FORMAT:
            raise ValueError("program manifest integrity check failed")
        recorded_sha = record.get("record_sha256")
        unsigned = {k: v for k, v in record.items() if k != "record_sha256"}
        if recorded_sha != hashlib.sha256(_canonical(unsigned)).hexdigest():
            raise ValueError("program metadata integrity check failed")
        hashes = record.get("files")
        if not isinstance(hashes, dict):
            raise ValueError("program file manifest integrity check failed")
        files = {}
        for name, expected in hashes.items():
            name = filename(name)
            try:
                data = (target / "files" / name).read_bytes()
            except FileNotFoundError as exc:
                raise ValueError("program byte integrity check failed") from exc
            if hashlib.sha256(data).hexdigest() != expected:
                raise ValueError("program byte integrity check failed")
            files[name] = data
        identity = {"entrypoint": record.get("entrypoint"),
                    "dependencies": record.get("dependencies"), "files": hashes}
        if "execution_platform" in record:
            if record["execution_platform"] not in ("windows", "linux"):
                raise ValueError("unsupported execution platform")
            identity["execution_platform"] = record["execution_platform"]
        if "observation_size" in record:
            if record["observation_size"] not in ([768, 461], [1000, 1000]):
                raise ValueError("unsupported program observation size")
            identity["observation_size"] = record["observation_size"]
        if record.get("id") != program_id or hashlib.sha256(
                _canonical(identity)).hexdigest() != program_id:
            raise ValueError("program identity integrity check failed")
        return record, files

    def list(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        records = []
        for path in self.root.iterdir():
            if path.is_dir() and len(path.name) == 64:
                records.append(self.get(path.name)[0])
        return sorted(records, key=lambda item: (item["created_at"], item["id"]))

    def finish(self, program_id: str) -> dict:
        record, _ = self.get(program_id)
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / f".final-{os.getpid()}.tmp"
        temp.write_text(json.dumps({"format": FORMAT, "id": program_id}, indent=2),
                        encoding="utf-8")
        os.replace(temp, self.root / "final.json")
        return record

    def final(self) -> dict | None:
        path = self.root / "final.json"
        if not path.is_file():
            return None
        try:
            pointer = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("invalid final program pointer") from exc
        if not isinstance(pointer, dict) or pointer.get("format") != FORMAT:
            raise ValueError("invalid final program pointer")
        return self.get(pointer.get("id"))[0]


__all__ = ["ProgramStore", "FORMAT"]
