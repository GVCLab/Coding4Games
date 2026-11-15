import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT=Path(__file__).resolve().parents[2]/"scripts"/"acceptance"/"wsl2_abnormal_cleanup_canary.py"
SPEC=importlib.util.spec_from_file_location("wsl2_abnormal_cleanup_canary",SCRIPT)
MODULE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


def test_preview_is_bounded_and_explicitly_has_no_model_or_game(tmp_path):
    profile=SimpleNamespace(id="f41f06710780")
    value=MODULE.preview(profile,tmp_path/"new-evidence")
    assert value["execute"] is False
    assert value["model_calls"] == 0 and value["game_started"] is False
    assert value["maximum_probe_wait_seconds"] <= 60


def test_abnormal_probe_exits_and_leaves_only_its_grandchild_in_scope():
    source=MODULE._probe_source()
    compile(source,"<abnormal-cleanup-probe>","exec")
    assert "os._exit(23)" in source
    assert "start_new_session=True" in source
    assert "time.sleep(300)" in source


def test_collect_requires_expected_abnormal_exit_and_one_owned_record():
    class Supervisor:
        def poll(self,handle):
            assert handle=="h"
            return {"running":False,"returncode":23,"output_complete":True,
                    "events":[{"stream":"stdout","data":"{\"parent_pid\": 10, \"grandchild_pid\": 11}\n"}]}
    owned, final=MODULE._collect_abnormal(Supervisor(),"h")
    assert owned=={"parent_pid":10,"grandchild_pid":11}
    assert final["returncode"]==23


def test_scope_membership_reads_only_valid_exact_nested_cgroup():
    calls=[]
    class Native:
        def exec(self,distro,argv,**kwargs):
            calls.append((distro,argv,kwargs)); return SimpleNamespace(stdout=b"44\n")
    path="/sys/fs/cgroup/c4gabc/child-0123456789abcdef0123456789abcdef"
    assert MODULE._scope_pids(Native(),"fresh",path)==[44]
    assert calls[0][1]==["/bin/cat",path+"/cgroup.procs"]
    with pytest.raises(ValueError,match="unsafe"):
        MODULE._scope_pids(Native(),"fresh","/sys/fs/cgroup/system.slice")


def test_scope_removal_check_is_exact_and_bounded():
    class Native:
        def exec(self,distro,argv,**kwargs):
            assert argv[-2:]==["scope-check","/sys/fs/cgroup/c4gx/child-a"]
            assert kwargs["timeout"]==5
            return SimpleNamespace(stdout=b"removed")
    assert MODULE._scope_removed(Native(),"fresh","/sys/fs/cgroup/c4gx/child-a") is True
