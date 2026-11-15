"""Host-owned save verification for outcomes that have no durable HUD signal."""
from __future__ import annotations

import json
import math
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from coding4game_gui.paths import REPO_ROOT


PALWORLD_TOOLKIT_FILES = ("docs/js/migrate.js", "docs/vendor/ooz-wasm/index.js")
PALWORLD_INSPECTOR_SCRIPT = Path(__file__).with_name("palworld_save_inspector.mjs")


@dataclass(frozen=True)
class SaveVerificationSpec:
    implementation: str
    poll_interval_seconds: float
    stable_seconds: float
    timeout_seconds: float
    toolkit_root: Path
    target_character_ids: tuple[str, ...]
    candidate_actions: tuple[str, ...]
    consumable_item_id: str


def parse_save_verification_spec(lifecycle: dict | None) -> SaveVerificationSpec | None:
    raw = (lifecycle or {}).get("save_verification")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("lifecycle.save_verification must be a mapping")
    implementation = str(raw.get("implementation") or "").strip().lower()
    if implementation != "palworld_capture":
        raise ValueError(
            "lifecycle.save_verification.implementation must be "
            "'palworld_capture'"
        )

    def finite_number(name: str, default: float, *, positive: bool) -> float:
        value = raw.get(name, default)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (value <= 0 if positive else value < 0)):
            qualifier = "positive" if positive else "nonnegative"
            raise ValueError(
                f"lifecycle.save_verification.{name} must be a finite "
                f"{qualifier} number"
            )
        return float(value)

    toolkit_raw = str(raw.get("toolkit_root") or "").strip()
    if not toolkit_raw:
        raise ValueError("lifecycle.save_verification.toolkit_root is required")
    toolkit_root = Path(toolkit_raw)
    if not toolkit_root.is_absolute():
        toolkit_root = (REPO_ROOT / toolkit_root).resolve()
    retired = {"required_new_palbox_count", "target_character_id"} & raw.keys()
    if retired:
        raise ValueError(f"Retired save verification options: {sorted(retired)}")
    targets_raw = raw.get("target_character_ids")
    if (not isinstance(targets_raw, list) or not targets_raw
            or not all(isinstance(item, str) and item.strip()
                       for item in targets_raw)):
        raise ValueError(
            "lifecycle.save_verification.target_character_ids must be a "
            "nonempty list of character IDs")
    targets = tuple(dict.fromkeys(item.strip() for item in targets_raw))
    timeout_seconds = finite_number(
        "timeout_seconds", 0.0, positive=False)
    actions_raw = raw.get("candidate_actions")
    if actions_raw is None:
        actions_raw = ["RB"] if timeout_seconds > 0 else []
    if (not isinstance(actions_raw, list)
            or (timeout_seconds > 0 and not actions_raw)
            or not all(isinstance(item, str) and item.strip()
                       for item in actions_raw)):
        raise ValueError(
            "lifecycle.save_verification.candidate_actions must be a list "
            "of action tokens and must be nonempty when timeout_seconds > 0"
        )
    return SaveVerificationSpec(
        implementation=implementation,
        poll_interval_seconds=finite_number(
            "poll_interval_seconds", 1.0, positive=True),
        stable_seconds=finite_number(
            "stable_seconds", 2.0, positive=False),
        timeout_seconds=timeout_seconds,
        toolkit_root=toolkit_root,
        target_character_ids=targets,
        candidate_actions=tuple(item.strip().upper() for item in actions_raw),
        consumable_item_id=str(raw.get("consumable_item_id") or "").strip(),
    )


class PalworldNodeInspector:
    """Read one Palworld world snapshot through the verified JS toolkit."""

    def __init__(self, spec: SaveVerificationSpec):
        self.spec = spec
        self.node = shutil.which("node")
        if not self.node:
            raise RuntimeError("Palworld save verification requires node.exe")
        self.script = PALWORLD_INSPECTOR_SCRIPT
        required = [self.spec.toolkit_root / name for name in PALWORLD_TOOLKIT_FILES]
        required.append(self.script)
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise RuntimeError(
                "Palworld save verification dependencies are missing: "
                + ", ".join(missing)
            )

    def __call__(self, level_path: Path, player_path: Path) -> dict:
        proc = subprocess.run(
            [
                self.node,
                str(self.script),
                str(self.spec.toolkit_root),
                str(level_path),
                str(player_path),
                *self.spec.target_character_ids,
                *([f"--inventory-item={self.spec.consumable_item_id}"]
                  if self.spec.consumable_item_id else []),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RuntimeError(
                f"Palworld save inspection failed ({proc.returncode}): {detail}"
            )
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Palworld save inspector returned invalid JSON") from exc
        if not isinstance(result, dict) or not isinstance(
                result.get("target_instances"), list):
            raise RuntimeError("Palworld save inspector returned invalid data")
        return result


class SaveVerificationMonitor:
    """Poll save metadata cheaply and parse only changed, stable snapshots."""

    def __init__(self, spec: SaveVerificationSpec, save_root: Path,
                 *, inspector: Callable[[Path, Path], dict] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.spec = spec
        self.save_root = Path(save_root)
        self.inspector = inspector or PalworldNodeInspector(spec)
        self.clock = clock
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._states: dict[Path, tuple[tuple[int, int], float,
                                      tuple[int, int] | None]] = {}
        self._baseline_ids: set[str] = set()
        self._baseline_consumable_count: int | None = None
        self._candidate_action_count = 0
        self._player_filename: str | None = None
        self._last_candidate_tic: int | None = None
        self._result: dict | None = None
        self._progress: dict | None = None
        self._completed = threading.Event()
        self._started_at: float | None = None
        self._error: str | None = None

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        return stat.st_size, stat.st_mtime_ns

    def _player_path_for(self, level_path: Path) -> Path | None:
        if self._player_filename is None:
            return None
        path = level_path.parent / "Players" / self._player_filename
        return path if path.is_file() else None

    def _candidates(self) -> list[Path]:
        paths = [self.save_root / "Level.sav"]
        backup_root = self.save_root / "backup" / "world"
        if backup_root.is_dir():
            paths.extend(path for path in backup_root.glob("*/Level.sav"))
        return sorted(
            (path for path in paths if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )

    def start(self) -> None:
        self.stop()
        level = self.save_root / "Level.sav"
        players = sorted((self.save_root / "Players").glob("*.sav"))
        players = [p for p in players if not p.name.endswith("_dps.sav")]
        if not level.is_file() or not players:
            raise RuntimeError(
                f"Palworld save verification cannot find Level.sav/player in "
                f"{self.save_root}"
            )
        baseline = self.inspector(level, players[0])
        self._baseline_ids = {
            str(item["instance_id"])
            for item in baseline["target_instances"]
            if item.get("in_palbox")
        }
        if self.spec.consumable_item_id:
            inventory = baseline.get("inventory_items") or {}
            value = inventory.get(self.spec.consumable_item_id)
            self._baseline_consumable_count = (
                int(value) if isinstance(value, (int, float)) else None)
        else:
            self._baseline_consumable_count = None
        self._player_filename = players[0].name
        self._candidate_action_count = 0
        now = self.clock()
        self._states = {}
        for path in self._candidates():
            fp = self._fingerprint(path)
            if fp is not None:
                self._states[path] = (fp, now, fp)
        self._result = None
        self._progress = {
            "status": "pending",
            "target_character_ids": list(self.spec.target_character_ids),
            "captured_target_character_ids": [],
            "new_instances": [],
            "consumable_item_id": self.spec.consumable_item_id or None,
            "consumable_baseline_count": self._baseline_consumable_count,
            "consumable_current_count": self._baseline_consumable_count,
            "consumable_inventory_consumed_count": 0,
            "consumable_candidate_action_count": (
                0 if self.spec.candidate_actions else None),
            "consumable_consumed_count": 0,
            "verified_at": None,
        }
        self._completed.clear()
        self._error = None
        # The timeout is the maximum wait for a save *after a capture attempt*.
        # Model/API thinking before the first RB must not consume this budget.
        self._started_at = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="palworld-save-verification",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.spec.poll_interval_seconds * 2))

    def note_action(self, buttons: list[str], tic: int) -> None:
        tokens = {str(button).upper() for button in buttons}
        if tokens.intersection(self.spec.candidate_actions):
            with self._lock:
                self._candidate_action_count += 1
                self._last_candidate_tic = int(tic)
                self._started_at = self.clock()

    def _run(self) -> None:
        while not self._stop.wait(self.spec.poll_interval_seconds):
            with self._lock:
                started_at = self._started_at
            if self.spec.timeout_seconds > 0 and started_at is not None and (
                    self.clock() - started_at >= self.spec.timeout_seconds):
                return
            try:
                self.scan_once()
            except Exception as exc:  # keep gameplay alive; expose audit error
                with self._lock:
                    self._error = f"{type(exc).__name__}: {exc}"

    def scan_once(self, *, force: bool = False) -> dict | None:
        with self._scan_lock:
            return self._scan_once_locked(force=force)

    def _scan_once_locked(self, *, force: bool = False) -> dict | None:
        if self.result is not None:
            return self.result
        now = self.clock()
        for path in self._candidates():
            fp = self._fingerprint(path)
            if fp is None:
                continue
            previous = self._states.get(path)
            if previous is None or previous[0] != fp:
                self._states[path] = (fp, now, None)
                if not force:
                    continue
                previous = self._states[path]
            current_fp, stable_since, parsed_fp = self._states[path]
            if parsed_fp == current_fp:
                continue
            if not force and now - stable_since < self.spec.stable_seconds:
                continue
            player = self._player_path_for(path)
            if player is None:
                continue
            snapshot = self.inspector(path, player)
            self._states[path] = (current_fp, stable_since, current_fp)
            added = [
                item for item in snapshot["target_instances"]
                if item.get("in_palbox")
                and str(item.get("instance_id")) not in self._baseline_ids
            ]
            found_targets = {str(item.get("character_id")) for item in added}
            captured_targets = [
                target for target in self.spec.target_character_ids
                if target in found_targets
            ]
            complete = len(captured_targets) == len(self.spec.target_character_ids)
            inventory = snapshot.get("inventory_items") or {}
            current_value = inventory.get(self.spec.consumable_item_id)
            current_count = (
                int(current_value)
                if isinstance(current_value, (int, float)) else None)
            inventory_consumed_count = (
                max(self._baseline_consumable_count - current_count, 0)
                if (self._baseline_consumable_count is not None
                    and current_count is not None) else None)
            with self._lock:
                candidate_action_count = (
                    self._candidate_action_count
                    if self.spec.candidate_actions else None)
            # Some Palworld save snapshots persist newly captured Pals before
            # the selected sphere stack is flushed.  The host already audits
            # every configured throw action, so use the larger of durable
            # inventory loss and candidate-action count.  Keeping both raw
            # values in evidence makes the fallback explicit and reviewable.
            consumed_count = max(
                value for value in (
                    inventory_consumed_count, candidate_action_count)
                if value is not None
            ) if (inventory_consumed_count is not None
                  or candidate_action_count is not None) else None
            progress = {
                "status": ("success" if complete else
                           "partial" if captured_targets else "pending"),
                "source": str(path),
                "target_character_ids": list(self.spec.target_character_ids),
                "captured_target_character_ids": captured_targets,
                "new_instances": added,
                "consumable_item_id": self.spec.consumable_item_id or None,
                "consumable_baseline_count": self._baseline_consumable_count,
                "consumable_current_count": current_count,
                "consumable_inventory_consumed_count":
                    inventory_consumed_count,
                "consumable_candidate_action_count": candidate_action_count,
                "consumable_consumed_count": consumed_count,
                "verified_at": time.time(),
            }
            with self._lock:
                current_count = len(
                    (self._progress or {}).get(
                        "captured_target_character_ids", []))
                if len(captured_targets) >= current_count:
                    self._progress = progress
            if not complete:
                continue
            with self._lock:
                self._result = dict(progress)
                self._completed.set()
                return dict(self._result)
        return None

    def final_check(self) -> dict | None:
        try:
            return self.scan_once(force=True)
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return None

    @property
    def result(self) -> dict | None:
        with self._lock:
            return None if self._result is None else dict(self._result)

    @property
    def progress(self) -> dict | None:
        with self._lock:
            return None if self._progress is None else dict(self._progress)

    @property
    def completed(self) -> bool:
        """Cheap cooperative-cancellation signal for a live action pulse."""
        return self._completed.is_set()

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error
