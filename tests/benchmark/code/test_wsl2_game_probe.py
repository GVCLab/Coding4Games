"""Pure safety and evidence checks; importing never starts a game or WSL."""
import subprocess
from types import SimpleNamespace

import pytest

from scripts.acceptance.wsl2_game_probe import observation_evidence_passed
from scripts.acceptance.wsl2_game_probe import cleanup_controller
from scripts.acceptance.wsl2_game_probe import validate_output_paths
from scripts.acceptance.wsl2_game_probe import validate_resolved_data_path


@pytest.mark.parametrize('overlap',['same','child','parent'])
def test_rejects_recursive_backup_and_restore_paths(tmp_path,overlap):
    data = tmp_path/'Balatro'
    out = {'same':data,'child':data/'probe','parent':tmp_path}[overlap]
    with pytest.raises(ValueError,match='disjoint'):
        validate_output_paths(data,out)


def test_accepts_disjoint_output(tmp_path):
    validate_output_paths(tmp_path/'Balatro',tmp_path/'evidence')


def test_accepts_only_normal_or_expected_msix_save_target(tmp_path):
    profile = tmp_path/'operator'
    appdata = profile/'AppData'/'Roaming'
    assert not validate_resolved_data_path(appdata/'Balatro',appdata,profile)
    packages = profile/'AppData'/'Local'/'Packages'
    data = packages/'OpenAI.Codex_example'/'LocalCache'/'Roaming'/'Balatro'
    assert validate_resolved_data_path(data,appdata,profile)
    with pytest.raises(ValueError,match='redirection'):
        validate_resolved_data_path(packages/'OtherApp'/'LocalCache'/'Roaming'/'Balatro',appdata,profile)
    with pytest.raises(ValueError,match='redirection'):
        validate_resolved_data_path(packages/'OpenAI.Codex_example'/'OtherData'/'Balatro',appdata,profile)


def fixture_rows():
    return [{'check':'start','image':{'dimensions':[1280,768],'sha256':'before'}},
            {'check':'stop','image':{'dimensions':[1280,768],'sha256':'after'}},
            {'check':'final_status'}]


def test_requires_images_and_visible_change_to_claim_acceptance():
    rows = fixture_rows()
    assert observation_evidence_passed(rows)
    rows[1]['image']['sha256'] = 'before'
    assert not observation_evidence_passed(rows)
    rows[0]['image'] = None
    assert not observation_evidence_passed(rows)


def test_requires_lifecycle_rows_and_correct_image_dimensions():
    rows = fixture_rows()
    assert not observation_evidence_passed(rows[:-1])
    rows[0]['image']['dimensions'] = [640,480]
    assert not observation_evidence_passed(rows)


def test_distro_termination_is_attempted_after_launcher_kill_failure(monkeypatch):
    commands = []
    def terminate(command,**kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('scripts.acceptance.wsl2_game_probe.subprocess.run',terminate)
    class BrokenLauncher:
        def kill(self):
            raise OSError('launcher disappeared')
        def communicate(self,**kwargs):
            raise subprocess.TimeoutExpired('launcher',10)
    errors = cleanup_controller(BrokenLauncher(),'C4G-WSL2-Probe-test')
    assert commands == [['wsl.exe','--terminate','C4G-WSL2-Probe-test']]
    assert errors == ['kill: OSError','drain: TimeoutExpired']
