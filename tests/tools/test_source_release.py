"""Verify publication boundaries against real archives, with fake credentials only."""
import hashlib
import json
import shutil
import subprocess
import zipfile

import pytest

from scripts.build_source_release import build, prepare


def source_tree(tmp_path):
    for name in ("LICENSE", "README.md", "pyproject.toml"):
        (tmp_path / name).write_text("public content\n")
    (tmp_path / "coding4game_gui").mkdir()
    (tmp_path / "coding4game_gui" / "public.py").write_text("print('hello')\n")
    return tmp_path


def test_source_archive_excludes_local_data_and_retains_verified_current_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source_tree(repo)
    originals = {}
    (repo / "coding4game_gui" / "benchmark" / "windows").mkdir(parents=True)
    inspector = "coding4game_gui/benchmark/windows/palworld_save_inspector.mjs"
    (repo / inspector).write_bytes(b"// Bundled inspector fixture\n")
    settings = "coding4game_gui/benchmark/windows/settings_templates/balatro-v1.lua"
    (repo / settings).parent.mkdir()
    (repo / settings).write_bytes(b"return {}\n")
    readme_media = ("docs/media/teaser.gif", "docs/media/preview.webp", "docs/media/demo.mp4")
    (repo / "docs/media").mkdir(parents=True)
    for relative in readme_media:
        (repo / relative).write_bytes(b"README media fixture")
    for relative in ("litellm.config.yaml", "launcher.local.json", "games.local.json", ".env",
                     "out/run.json", "outputs/report.json", "snapshots/golden.zip", "runtime/profile.json",
                     "docs/raw-capture.mp4", "docs/raw-capture.gif", "out/run.mp4",
                     "requirements.txt", "requirements-dev.txt", "integrations/old_adapter.mjs",
                     "coding4game_gui/private.local.json", "coding4game_gui/auth.json",
                     "coding4game_gui/__pycache__/private.py", "coding4game_gui/vendor/node_modules/private.py"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"private fixture data")
        originals[path] = path.read_bytes()
    archive = tmp_path / "release.zip"
    result = build(repo, archive)
    assert result["payload_verified"] is True
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert set(names) == {"Coding4Game_GUI/" + name for name in (
            "LICENSE", "README.md", "pyproject.toml", "coding4game_gui/public.py",
            inspector, settings, *readme_media, "SOURCE_RELEASE.json")}
        manifest = json.loads(bundle.read("Coding4Game_GUI/SOURCE_RELEASE.json"))
        for entry in manifest["files"]:
            assert hashlib.sha256(bundle.read("Coding4Game_GUI/" + entry["path"])).hexdigest() == entry["sha256"]
    assert all(path.read_bytes() == data for path, data in originals.items())
    with pytest.raises(FileExistsError):
        build(repo, archive)


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required")
def test_checkout_release_ignores_untracked_yaml_docs_and_tools(tmp_path):
    repo = source_tree(tmp_path)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "--", "."], check=True)
    for relative in ("games/kof98_2p.yaml", "coding4game_gui/benchmark/scenarios/gta_v.yaml",
                     "docs/design/draft.md", "scripts/reports/draft.py", ".claude/skills/draft/SKILL.md"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local draft\n", encoding="utf-8")

    manifest, _ = prepare(repo)

    assert {entry["path"] for entry in manifest["files"]} == {
        "LICENSE", "README.md", "pyproject.toml", "coding4game_gui/public.py",
    }


def test_release_rejects_accidental_secret_without_echoing_it(tmp_path):
    repo = source_tree(tmp_path)
    path = repo / "coding4game_gui" / "mistake.py"
    for secret in ("sk-" + "x" * 32, "github_pat_" + "x" * 60,
                   "AIza" + "x" * 35, "hf_" + "x" * 28):
        path.write_text("token = '" + secret + "'\n")
        with pytest.raises(ValueError) as error:
            prepare(repo)
        assert "mistake.py" in str(error.value) and secret not in str(error.value)


def test_release_rejects_links_to_files_outside_source_tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    source_tree(repo)
    outside = tmp_path / "private.py"
    outside.write_text("do not export")
    try:
        (repo / "coding4game_gui" / "linked.py").symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(ValueError, match="linked file"):
        prepare(repo)
