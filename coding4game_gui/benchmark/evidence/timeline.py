"""Append-only development evidence and a small static audit view."""
from __future__ import annotations

import difflib
import html
import json
import os
from pathlib import PurePosixPath
import re
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from coding4game_gui.benchmark.evidence._lock import locked


class DevelopmentTimeline:
    def __init__(self, path: Path, *, secrets=(), max_inline_chars: int = 4096):
        self.path = Path(path)
        self.secrets = tuple(str(item) for item in secrets if str(item))
        if type(max_inline_chars) is not int or max_inline_chars < 16:
            raise ValueError("max_inline_chars must be at least 16")
        self.max_inline_chars = max_inline_chars

    def _text(self, value: str) -> str:
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        if len(value) > self.max_inline_chars:
            value = value[:self.max_inline_chars - len("[TRUNCATED]")] + "[TRUNCATED]"
        return value

    def _safe(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, list):
            return [self._safe(item) for item in value[:1000]]
        if isinstance(value, dict):
            return {self._text(str(key)): self._safe(item)
                    for key, item in list(value.items())[:1000]}
        return self._text(str(value))

    def _ref(self, value: str) -> str:
        """Accept only bounded, portable paths relative to the report root."""
        if (not value or len(value) > self.max_inline_chars or "\\" in value
                or re.search(r'[\x00-\x1f<>:"|?*]', value)):
            raise ValueError("evidence ref must be a safe report-relative path")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("evidence ref must be a safe report-relative path")
        return path.as_posix()

    def read(self) -> list[dict]:
        if not self.path.is_file():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def append(self, kind: str, *, source: str = "harness", summary: str = "",
               details: dict | None = None, refs=()) -> dict:
        if not isinstance(kind, str) or not kind:
            raise ValueError("timeline kind must be nonempty")
        if source not in {"harness", "sandbox", "external_observation", "agent_claim"}:
            raise ValueError("unknown timeline evidence source")
        if not isinstance(details, (dict, type(None))):
            raise ValueError("timeline details must be an object")
        if not isinstance(refs, (list, tuple)) or any(not isinstance(x, str) for x in refs):
            raise ValueError("timeline refs must be strings")
        safe_refs = [self._ref(ref) for ref in refs[:1000]]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        with self.path.open("a+", encoding="utf-8") as handle:
            with locked(handle):
                handle.seek(0)
                count = highest = 0
                for line in handle:
                    if not line.strip(): continue
                    count += 1
                    try: previous = json.loads(line)
                    except ValueError: continue
                    value = previous.get("seq") if isinstance(previous, dict) else None
                    if type(value) is int: highest = max(highest, value)
                seq = max(count, highest) + 1
                row = {
                    "seq": seq, "ts": now,
                    "iso_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                    "kind": self._text(kind), "source": source,
                    "summary": self._text(str(summary)),
                    "details": self._safe(details or {}),
                    "refs": safe_refs,
                    "pid": os.getpid(), "thread_id": threading.get_ident(),
                }
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        return row

    def self_note(self, text: str) -> dict:
        return self.append("self_note", source="agent_claim", summary=text)

    def submission(self, record: dict, *, event: str) -> dict:
        return self.append("submission", summary=event,
                           details={"event": event, "program": record})

    def _local_artifact(self, ref: str, out: Path) -> tuple[Path, str] | None:
        """Resolve a stored safe ref without allowing symlink/path escape."""
        try:
            ref = self._ref(ref)
            root = self.path.parent.resolve()
            path = (root / Path(*PurePosixPath(ref).parts)).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                return None
            relative = os.path.relpath(path, out.parent.resolve()).replace("\\", "/")
            return path, quote(relative, safe="/")
        except (OSError, ValueError):
            return None

    def _render_ref(self, ref: str, out: Path) -> str:
        label = html.escape(ref)
        local = self._local_artifact(ref, out)
        if local is None:
            return f'<span class="missing" title="artifact not found">{label}</span>'
        _path, href = local
        safe_href = html.escape(href, quote=True)
        suffix = PurePosixPath(ref).suffix.casefold()
        link = f'<a href="{safe_href}">{label}</a>'
        if suffix in {".png", ".jpg", ".jpeg"}:
            return link + f'<br><img src="{safe_href}" loading="lazy" alt="{label}">'
        if suffix == ".mp4":
            return link + (f'<br><video controls preload="metadata" '
                           f'src="{safe_href}"></video>')
        return link

    def _submission_diff(self, before: dict, after: dict) -> str:
        def source(record):
            entry = record.get("entrypoint") or {}
            argv = entry.get("argv") or []
            if not argv or not isinstance(argv[0], str):
                return None
            ref = f"programs/{record.get('id')}/files/{argv[0]}"
            local = self._local_artifact(ref, self.path.parent / "timeline.html")
            if local is None or local[0].suffix.casefold() not in {
                    ".py", ".txt", ".json", ".toml", ".yaml", ".yml", ".md"}:
                return None
            if local[0].stat().st_size > 512_000:
                return None
            try:
                return argv[0], local[0].read_text(encoding="utf-8").splitlines(True)
            except UnicodeDecodeError:
                return None
        left, right = source(before), source(after)
        if left is None or right is None:
            return ""
        diff = "".join(difflib.unified_diff(
            left[1], right[1],
            fromfile=f"{before.get('id')}/{left[0]}",
            tofile=f"{after.get('id')}/{right[0]}"))
        if len(diff) > 20_000:
            diff = diff[:19_988] + "[TRUNCATED]"
        return diff

    def render_html(self, out: Path, *, submissions=()) -> Path:
        out = Path(out)
        submissions = list(submissions)
        rows = self.read()
        row_html = []
        for row in rows:
            refs = "<br>".join(self._render_ref(ref, out)
                                for ref in row.get("refs", []))
            source = ("Agent claim" if row.get("source") == "agent_claim"
                      else str(row.get("source", "")))
            row_html.append(
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td><pre>{}</pre></td></tr>".format(
                    html.escape(str(row.get("seq", ""))),
                    html.escape(source),
                    html.escape(str(row.get("kind", ""))),
                    html.escape(str(row.get("summary", ""))),
                    html.escape(json.dumps(row.get("details", {}), ensure_ascii=False))
                    + ("<br>" + refs if refs else "")))
        programs = []
        for item in submissions:
            ident = str(item.get("id", ""))
            verified = bool((item.get("reproducibility") or {}).get(
                "dependencies_verified"))
            dependencies = item.get("dependencies") or {}
            dependency_status = ("verified" if verified else
                                 "declared, unverified" if dependencies else
                                 "none declared")
            file_links = []
            for name in (item.get("files") or {}):
                ref = f"programs/{ident}/files/{name}"
                file_links.append(self._render_ref(ref, out))
            programs.append(
                "<li><code>{}</code> — {}<br>entrypoint: <code>{}</code><br>"
                "dependencies: {}<br>files: {}</li>".format(
                    html.escape(ident), html.escape(str(item.get("description", ""))),
                    html.escape(json.dumps(item.get("entrypoint", {}),
                                           ensure_ascii=False)),
                    html.escape(dependency_status), ", ".join(file_links)))
        diffs = []
        for before, after in zip(submissions, submissions[1:]):
            diff = self._submission_diff(before, after)
            if diff:
                diffs.append("<h3><code>{}</code> → <code>{}</code></h3><pre>{}</pre>".format(
                    html.escape(str(before.get("id", ""))),
                    html.escape(str(after.get("id", ""))), html.escape(diff)))
        codex = self._local_artifact("codex_events.jsonl", out)
        codex_index = "<p>No full Codex stdout event file is present.</p>"
        if codex is not None:
            with codex[0].open(encoding="utf-8", errors="replace") as stream:
                count = sum(1 for line in stream if line.strip())
            noun = "event" if count == 1 else "events"
            codex_index = (f'<p><a href="{html.escape(codex[1], quote=True)}">'
                           f'codex_events.jsonl</a> — {count} full stdout {noun}.</p>')
        document = """<!doctype html><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self'; media-src 'self'; style-src 'unsafe-inline'"><title>Development timeline</title>
<style>body{{font:14px system-ui;margin:2rem;color:#18202a}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccd3da;padding:.5rem;vertical-align:top}}pre{{white-space:pre-wrap;margin:0;max-height:32rem;overflow:auto}}code{{word-break:break-all}}.limits{{background:#fff6db;padding:1rem}}img{{max-width:480px;max-height:300px}}video{{max-width:560px;max-height:340px}}.missing{{color:#777}}</style>
<h1>Development timeline</h1><div class="limits"><strong>Evidence limitations.</strong>
Agent-authored self-notes are unverified claims. Dependency declarations are not proof that dependencies were verified or reproduced. This audit cannot prove the absence of a local or embedded LLM. Timeline summaries are bounded and may be truncated; they are not the full Codex stdout record.</div>
<h2>Full Codex stdout index</h2>{}<h2>Submissions</h2><ul>{}</ul><h2>Submitted source diffs</h2>{}<h2>Events</h2><table><thead><tr><th>#</th><th>Source</th><th>Kind</th><th>Summary</th><th>Details / refs</th></tr></thead><tbody>{}</tbody></table>""".format(
            codex_index, "".join(programs), "".join(diffs) or "<p>No comparable text entrypoints.</p>",
            "".join(row_html))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(document, encoding="utf-8")
        return out


__all__ = ["DevelopmentTimeline"]
