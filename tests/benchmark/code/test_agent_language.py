"""Guard authored English text without rewriting localized evidence or inputs."""
import ast
import io
import re
import tokenize
from pathlib import Path

import yaml

from coding4game_gui.benchmark.code.tool_bridge import tools_for_phase


ROOT = Path(__file__).resolve().parents[3]
HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U000323af]")


def assert_english(value, source):
    # These are exact labels from the localized game UI, kept as task data.
    for label in ("召唤援军", "箭矢风暴"):
        value = value.replace(label, "")
    # This catches the Chinese-text regression, not arbitrary language misuse.
    assert not HAN.search(value), f"Authored text must be English: {source}"


def test_agent_instructions_and_runtime_modules_are_english():
    paths = [ROOT / "AGENTS.md"]
    paths += list((ROOT / ".agents").rglob("*.md"))
    paths += list((ROOT / "coding4game_gui/benchmark/code").rglob("*.md"))
    paths += list((ROOT / "coding4game_gui/benchmark/code").rglob("*.py"))
    paths += list((ROOT / "coding4game_gui/runtime_client").rglob("*.py"))
    paths += list((ROOT / "coding4game_gui/wsl2").rglob("*.py"))
    for path in paths:
        assert_english(path.read_text(encoding="utf-8-sig"), path.relative_to(ROOT))
    for phase in ("development", "final_test"):
        assert_english(str(tools_for_phase(phase)), f"{phase} tool schema")


def test_python_comments_and_docstrings_are_english():
    for directory in ("coding4game_gui", "scripts", "tests", ".agents"):
        for path in (ROOT / directory).rglob("*.py"):
            source = path.read_text(encoding="utf-8-sig")
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    assert_english(token.string, f"{path}:{token.start[0]}")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    assert_english(ast.get_docstring(node) or "", path)


def test_yaml_comments_and_descriptions_are_english():
    def descriptions(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "description":
                    yield str(child)
                yield from descriptions(child)
        elif isinstance(value, list):
            for child in value:
                yield from descriptions(child)

    for directory in ("games", "coding4game_gui/benchmark/scenarios"):
        for path in (ROOT / directory).glob("*.yaml"):
            source = path.read_text(encoding="utf-8-sig")
            # Comments here are standalone or preceded by whitespace. OCR values
            # are intentionally not language-filtered; they must match the game.
            for line in source.splitlines():
                if line.lstrip().startswith("#"):
                    assert_english(line, path)
            for description in descriptions(yaml.safe_load(source)):
                assert_english(description, path)
