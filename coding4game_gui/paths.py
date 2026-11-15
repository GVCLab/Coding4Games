"""Project data and packaged runtime resources; never discovers user secrets."""
from importlib.resources import files
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent
GAMES_DIR = REPO_ROOT / "games"
SCENARIO_DIR = PACKAGE_ROOT / "benchmark" / "scenarios"


def runtime_source(name: str) -> bytes:
    if name not in {"client.py", "wire.py", "bundle.py"}:
        raise ValueError("unknown runtime resource")
    return files("coding4game_gui.runtime_client").joinpath(name).read_bytes()


def development_template() -> str:
    return files("coding4game_gui.benchmark.code").joinpath("prompts/development.md").read_text(encoding="utf-8")


def no_reparse(path):
    """Reject symlinks and Windows reparse points in an operator path."""
    for item in (Path(path), *Path(path).parents):
        if item.exists() and (item.is_symlink() or
                getattr(item.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError(f"operator path contains a reparse point: {item}")
