from pathlib import Path
from types import SimpleNamespace

import pytest

from coding4game_gui.benchmark.windows import steam_paths
from coding4game_gui.benchmark.windows import game_launch


def test_relocation_reads_an_external_library_and_preserves_exact_game_path(tmp_path, monkeypatch):
    steam = tmp_path / "Steam"
    library = tmp_path / "External Library"
    (steam / "steamapps").mkdir(parents=True)
    game = library / "steamapps" / "common" / "Demo" / "bin" / "Demo.exe"
    game.parent.mkdir(parents=True)
    game.write_bytes(b"MZ")
    escaped = str(library).replace("\\", "\\\\")
    (steam / "steamapps/libraryfolders.vdf").write_text(
        '"libraryfolders" { "1" { "path" "' + escaped + '" } }')
    monkeypatch.setattr(steam_paths, "steam_install_roots", lambda: [steam])
    old = tmp_path / "Old Library/steamapps/common/Demo/bin/Demo.exe"
    resolved = game_launch.resolve_launch_path(SimpleNamespace(launch_path=str(old)))
    assert resolved == game.resolve()


def test_relocation_rejects_ambiguous_installed_copies(tmp_path):
    roots = [tmp_path / "A", tmp_path / "B"]
    for root in roots:
        exe = root / "steamapps/common/Demo/Demo.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
    with pytest.raises(ValueError, match="multiple installed"):
        steam_paths.relocated_executable(
            Path("C:/Missing/steamapps/common/Demo/Demo.exe"), install_roots=roots)


def test_relocation_cannot_escape_library_or_guess_an_unrelated_install(tmp_path):
    assert steam_paths.relocated_executable(
        Path("C:/Missing/steamapps/common/../Demo.exe"), install_roots=[tmp_path]) is None
    assert steam_paths.relocated_executable(
        Path("C:/Unrelated/Demo.exe"), install_roots=[tmp_path]) is None


def test_steam_checkpoint_uses_active_profile_and_fails_without_login(monkeypatch, tmp_path):
    from coding4game_gui.benchmark.windows.save_state import _expand_path

    active = tmp_path / "Steam/userdata/42"
    monkeypatch.setattr(steam_paths, "steam_active_userdata_root", lambda: active)
    assert _expand_path("%STEAM_USERDATA%/238460") == active / "238460"

    def no_login():
        raise RuntimeError("Steam has no active account")

    monkeypatch.setattr(steam_paths, "steam_active_userdata_root", no_login)
    with pytest.raises(RuntimeError, match="no active account"):
        _expand_path("%STEAM_USERDATA%/238460")
    assert "%STEAM_USERDATA%" in str(_expand_path(
        "%STEAM_USERDATA%/238460", validate_files=False))
