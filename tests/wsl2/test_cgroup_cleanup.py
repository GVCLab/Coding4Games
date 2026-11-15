from types import SimpleNamespace
import pytest

from coding4game_gui.wsl2.cgroup_cleanup import cleanup,wait_empty
from coding4game_gui.wsl2.native import WslNative


def test_wait_reads_populated_state_until_processes_exit(tmp_path):
    events=tmp_path/'cgroup.events'
    events.write_text('populated 1\nfrozen 0\n')
    slept=[]
    def sleep(seconds):
        slept.append(seconds)
        events.write_text('populated 0\nfrozen 0\n')
    wait_empty(tmp_path,clock=lambda:0,sleep=sleep)
    assert slept==[.05]


def test_populated_cgroup_timeout_never_allows_removal(tmp_path):
    (tmp_path/'cgroup.events').write_text('populated 1\n')
    ticks=iter([0,5])
    with pytest.raises(TimeoutError,match='remains populated'):
        wait_empty(tmp_path,clock=lambda:next(ticks),sleep=lambda _:pytest.fail('deadline passed'))
    assert tmp_path.is_dir()


@pytest.mark.parametrize('path',['/sys/fs/cgroup','/sys/fs/cgroup/system.slice',
    '/sys/fs/cgroup/c4g0123456789ab/../system.slice',
    '/sys/fs/cgroup/c4g0123456789ab/child-unsafe;command'])
def test_cleanup_rejects_foreign_or_injected_paths(path):
    with pytest.raises(ValueError,match='unsafe'): cleanup(path,kill=True)


def test_native_cleanup_does_not_delete_network_policy_before_group_removal(monkeypatch):
    native=WslNative(); calls=[]
    def fail(name,argv,**kwargs):
        calls.append(argv)
        raise RuntimeError('owned group remains populated')
    monkeypatch.setattr(native,'exec',fail)
    with pytest.raises(RuntimeError,match='populated'):
        native.teardown('test-only',{'table':'c4g0123456789ab',
            'cgroup':'/sys/fs/cgroup/c4g0123456789ab'})
    assert len(calls)==1 and 'nft' not in calls[0]


def test_native_cleanup_uses_pinned_nft_path_and_accepts_already_removed_table(monkeypatch):
    native=WslNative(); calls=[]
    def execute(name,argv,**kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=b'{"nftables":[]}')
    monkeypatch.setattr(native,'exec',execute)
    native.teardown('test-only',{'table':'c4g0123456789ab',
        'cgroup':'/sys/fs/cgroup/c4g0123456789ab'})
    assert calls[1]==['/usr/sbin/nft','-j','list','tables'] and len(calls)==2
