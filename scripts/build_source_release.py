"""Preview or build a source-only release, without exporting local experiments."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

REPO = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "LICENSE", "AGENTS.md", ".gitattributes", ".gitignore", "pyproject.toml",
    "snapshots/README.md",
}
SOURCE_DIRS = ("coding4game_gui", "games", "scripts", "tests", "docs", ".agents", ".github")
EXTENSIONS = {".py", ".ps1", ".md", ".yaml", ".yml", ".json", ".toml", ".txt",
              ".png", ".jpg", ".npz", ".mjs", ".js", ".lua", ".cpp", ".c", ".cs", ".h", ".ini"}
README_MEDIA_DIR = Path("docs/media")
README_MEDIA_EXTENSIONS = {".gif", ".webp", ".mp4"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", "out", "outputs"}
HISTORICAL_DOCS = {"history", "research", "experiments"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(rb"(?<![\w-])sk-(?:proj-|ant-|or-|svcacct-)?[A-Za-z0-9_-]{24,}(?![\w-])"),
    re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{45,})\b"),
    re.compile(rb"\b(?:AIza[A-Za-z0-9_-]{35}|hf_[A-Za-z0-9]{24,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{12,}\b"),
)


def private_name(name: str) -> bool:
    lower = name.lower()
    return (lower.startswith(".env") or ".local." in lower or ".bak" in lower
            or lower in {"auth.json", "credentials.json", "litellm.config.yaml"}
            or lower.endswith((".pem", ".key")))


def tracked_paths(repo: Path) -> set[Path] | None:
    if not (repo / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "ls-files", "--cached", "-z"],
            capture_output=True, check=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("Cannot read tracked release files from Git.") from error
    return {Path(os.fsdecode(name)) for name in result.stdout.split(b"\0") if name}


def source_files(repo: Path) -> list[Path]:
    repo = Path(repo).resolve()
    tracked = tracked_paths(repo)
    tracked_dirs = None if tracked is None else {parent for path in tracked for parent in path.parents}
    candidates = [repo / name for name in ROOT_FILES
                  if (tracked is None or Path(name) in tracked) and (repo / name).is_file()]
    for directory in SOURCE_DIRS:
        base = repo / directory
        if (tracked_dirs is not None and Path(directory) not in tracked_dirs) or not base.exists():
            continue
        if base.is_symlink() or getattr(base.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Source release refuses a linked directory: {directory}")
        for current, folders, files in os.walk(base, followlinks=False):
            current = Path(current)
            relative = current.relative_to(repo)
            folders[:] = [
                name for name in folders
                if name not in SKIP_DIRS and not private_name(name)
                and (tracked_dirs is None or relative / name in tracked_dirs)
                and not (directory == "docs" and current == base and name in HISTORICAL_DOCS)
            ]
            for name in folders:
                item = current / name
                if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                    raise ValueError(f"Source release refuses a linked directory: {item.relative_to(repo)}")
            extensions = EXTENSIONS
            if relative == README_MEDIA_DIR:
                extensions = EXTENSIONS | README_MEDIA_EXTENSIONS
            candidates.extend(
                current / name for name in files
                if Path(name).suffix.lower() in extensions and not private_name(name)
                and (tracked is None or relative / name in tracked)
            )
    for item in candidates:
        if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Source release refuses a linked file: {item.relative_to(repo)}")
        if not item.is_file():
            raise ValueError("Source release accepts regular files only.")
    return sorted(set(candidates), key=lambda p: p.relative_to(repo).as_posix())


def prepare(repo: Path) -> tuple[dict, list[tuple[str, bytes]]]:
    repo = Path(repo).resolve()
    paths = source_files(repo)
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        if repo / name not in paths:
            raise ValueError(f"Missing required release file: {name}")
    records, payloads = [], []
    for path in paths:
        relative = path.relative_to(repo).as_posix()
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f"Source file exceeds 64 MiB: {relative}")
        raw = path.read_bytes()
        if path.suffix.lower() not in {".npz", ".png", ".jpg"}:
            if any(pattern.search(raw) for pattern in SECRET_PATTERNS):
                raise ValueError(f"Potential credential material in release file: {relative}; inspect locally.")
        records.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        payloads.append((relative, raw))
    def git(*args):
        if not (repo / ".git").exists():
            return None
        try:
            result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                    text=True, timeout=10)
            return result.stdout.strip() if result.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    status = git("status", "--porcelain")
    manifest = {"format": "c4g-source-release-v1", "source_head": git("rev-parse", "HEAD"),
                "source_has_uncommitted_changes": None if status is None else bool(status),
                "files": records, "file_count": len(records),
                "bytes": sum(item["bytes"] for item in records),
                "excluded": ["Git history", "local credentials/settings", "run evidence", "save archives",
                             "WSL images/profiles", "installed binaries", "historical reports"],
                "credential_check_scope": "Filename exclusions and high-confidence signatures; not a Git-history audit."}
    return manifest, payloads


def build(repo: Path, destination: Path) -> dict:
    manifest, payloads = prepare(repo)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation preserves any previous release, including on a retry.
    with destination.open("xb") as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, raw in payloads:
                archive.writestr("Coding4Game_GUI/" + relative, raw)
            archive.writestr("Coding4Game_GUI/SOURCE_RELEASE.json", json.dumps(manifest, indent=2) + "\n")
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None:
            raise ValueError("Source archive verification failed; preserve the failed archive for inspection.")
        for item in manifest["files"]:
            raw = archive.read("Coding4Game_GUI/" + item["path"])
            if len(raw) != item["bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("Source archive payload verification failed.")
    return {"archive": str(destination.resolve()), "file_count": manifest["file_count"],
            "source_bytes": manifest["bytes"], "archive_bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "payload_verified": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "Coding4Game_GUI-source.zip")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            report = build(REPO, args.out)
        else:
            manifest, _ = prepare(REPO)
            report = {key: value for key, value in manifest.items() if key != "files"}
            report["preview_only"] = True
        print(json.dumps(report, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
