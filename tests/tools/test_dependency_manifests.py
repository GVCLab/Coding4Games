from __future__ import annotations

import tomllib
from pathlib import Path

from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]


def _requirement_names(requirements: list[str]) -> set[str]:
    return {Requirement(entry).name.lower() for entry in requirements}


def test_pyproject_covers_host_and_test_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    runtime = _requirement_names(project["dependencies"])

    expected = {
        "litellm",
        "opencv-contrib-python",
        "packaging",
        "pyyaml",
        "ruamel.yaml",
    }
    assert expected <= runtime
    assert runtime.isdisjoint({"vizdoom", "imageio-ffmpeg", "pywinctl", "av", "claude-agent-sdk", "httpx"})
    optional = project["optional-dependencies"]
    assert "pytest" in _requirement_names(optional["dev"])
    ocr = {"paddleocr", "paddlepaddle-gpu"}
    assert ocr == _requirement_names(optional["ocr"])
    assert runtime.isdisjoint(ocr)
    assert runtime.isdisjoint({"opencv-python", "opencv-python-headless", "opencv-contrib-python-headless"})
    requirements = list(map(Requirement, project["dependencies"]))
    litellm = next(item for item in requirements if item.name == "litellm")
    assert not litellm.extras
    paddle = [item for item in map(Requirement, optional["ocr"]) if item.name == "paddlepaddle-gpu"]
    for version in ("3.11", "3.12", "3.13"):
        active = [item for item in paddle if item.marker.evaluate({
            "sys_platform": "win32", "python_version": version,
        })]
        tag = "cp" + version.replace(".", "")
        assert len(active) == 1
        assert active[0].url.endswith(f"paddlepaddle_gpu-3.3.1-{tag}-{tag}-win_amd64.whl")
