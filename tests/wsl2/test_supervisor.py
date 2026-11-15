from pathlib import Path, PurePosixPath
import socket
import pytest


def test_failed_child_cleanup_keeps_handle_for_close_retry(monkeypatch):
    from types import SimpleNamespace
    s=object.__new__(WslSupervisor)
    s.distro='test-only'
    path='/sys/fs/cgroup/c4g0123456789ab/child-'+'a'*32
    s._child_cgroups={'owned-handle':path}
    s.call=lambda *a,**kw:{'terminated':True,'exit_code':0}
    def fail(*args): raise RuntimeError('cgroup is still populated')
    s.runtime=SimpleNamespace(native=SimpleNamespace(kill_child_cgroup=fail))
    with pytest.raises(RuntimeError,match='still populated'):
        s.terminate('owned-handle')
    assert s._child_cgroups=={'owned-handle':path}

from coding4game_gui.wsl2.supervisor import (WslSupervisor, _ISOLATE, _handshake_error,
                                             _bind_control_listener,
                                             _mount_isolation_from_hello, _resolver_target_scope)
from coding4game_gui.wsl2.native import WslNative
from coding4game_gui.wsl2.runtime_error import WslTransportError


def bare(role="development"):
    sup = object.__new__(WslSupervisor); sup.role=role; sup.closed=False
    sup._dependency_preparing=False; sup._dependency_failed=False
    sup._final_execution_started=False; sup._dependency_manager=None
    sup._child_cgroups={}
    sup.mount_isolation={"mount_namespace":"mnt:[2]"}
    sup.workspace=PurePosixPath("/home/c4gprobe/run")
    sup.runtime=type("R",(),{"profile":type("P",(),{
        "python":PurePosixPath("/usr/bin/python3"), "ports":{"gateway":51005}, "uid":1000,
        "agent":lambda self,n: PurePosixPath("/opt/c4g-tools/claude")})()})()
    return sup


class BindError(OSError):
    def __init__(self, code):
        super().__init__(code, "bind failed")
        self.winerror = code


class BindSocket:
    def __init__(self, outcomes, calls):
        self.outcomes=outcomes; self.calls=calls; self.closed=False
    def setsockopt(self,*args): self.calls.append(("setsockopt",args))
    def bind(self,address):
        self.calls.append(("bind",address))
        outcome=self.outcomes.pop(0)
        if outcome is not None: raise outcome
    def close(self): self.closed=True; self.calls.append(("close",))


def test_control_listener_retries_windows_access_collision_then_binds_same_fixed_endpoint():
    outcomes=[BindError(10013),None]; calls=[]; sockets=[]
    def factory():
        value=BindSocket(outcomes,calls); sockets.append(value); return value
    listener,readiness=_bind_control_listener(
        3438,bind_timeout=10,socket_factory=factory,monotonic=lambda:0,sleep=lambda _:None)
    assert listener is sockets[1] and sockets[0].closed is True and sockets[1].closed is False
    assert [value for value in calls if value[0]=="bind"]==[
        ("bind",("127.0.0.1",3438)),("bind",("127.0.0.1",3438))]
    assert readiness=={"host":"127.0.0.1","port":3438,"attempts":2,"bind_wait_seconds":0.0}
    assert all(value[1][:2]==(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE)
               for value in calls if value[0]=="setsockopt")
    listener.close()


def test_control_listener_permanent_windows_collision_expires_with_safe_diagnostic():
    times=iter((0,0,1,2)); sockets=[]
    def factory():
        value=BindSocket([BindError(10048)],[]); sockets.append(value); return value
    with pytest.raises(WslTransportError,match=r"127\.0\.0\.1:3438.*attempts=2.*winerror=10048"):
        _bind_control_listener(3438,bind_timeout=1,socket_factory=factory,
                               monotonic=lambda:next(times),sleep=lambda _:None)
    assert len(sockets)==2 and all(value.closed for value in sockets)


def test_control_listener_does_not_retry_non_windows_or_unexpected_bind_errors():
    for error in (PermissionError(13,"denied"),BindError(10049)):
        calls=[]; value=BindSocket([error],calls)
        with pytest.raises(type(error)):
            _bind_control_listener(3438,bind_timeout=10,socket_factory=lambda:value,
                                   monotonic=lambda:0,sleep=lambda _:pytest.fail("must not retry"))
        assert value.closed is True


def test_supervisor_acquires_control_listener_before_starting_worker():
    source=Path(WslSupervisor.__init__.__code__.co_filename).read_text(encoding="utf-8")
    assert source.index("_bind_control_listener(port)") < source.index("subprocess.Popen(command")


def test_native_path_is_posix_and_cannot_escape():
    s=bare()
    assert s.native_path(s.workspace,"a","b.py")=="/home/c4gprobe/run/a/b.py"
    with pytest.raises(ValueError): s.native_path(s.workspace,"../escape")


def test_stage_uses_bounded_bundle_rpc(monkeypatch):
    s=bare(); seen={}
    monkeypatch.setattr(s,"call",lambda op,args,timeout=60: seen.update(op=op,args=args) or {"path":"/home/c4gprobe/run/x"})
    assert s.stage({"a.py":b"x"},"candidate")==PurePosixPath("/home/c4gprobe/run/x")
    assert seen["op"]=="stage" and seen["args"]["files"]["a.py"]=="eA=="


def test_final_launch_rejects_model_or_secret_environment_before_rpc(monkeypatch):
    s=bare("final_test")
    monkeypatch.setattr(s,"call",lambda *a,**k: pytest.fail("no RPC"))
    for env in ({"ANTHROPIC_API_KEY":"x"},{"SOME_PASSWORD":"x"},{"CLAUDE_CONFIG_DIR":"/x"},
                {"API_TIMEOUT_MS":"1860000"}, {"CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS":"1800000"},
                {"CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS":"1800000"},
                {"CLAUDE_STREAM_IDLE_TIMEOUT_MS":"1800000"},
                {"BUN_CONFIG_HTTP_IDLE_TIMEOUT":"1800"}):
        with pytest.raises(ValueError,match="forbidden"): s.launch(["/usr/bin/python3"],env=env)


def test_only_claude_agent_and_scoped_bridge_environment(monkeypatch):
    s=bare(); captured={}
    monkeypatch.setattr(s,"_launch",lambda argv,**kw: captured.update(argv=argv,**kw) or {"handle":"h"})
    lease={"url":"http://127.0.0.1:51005/v1","token":"x"*32,"port":51005}
    s.launch_agent("claude",["/opt/c4g-tools/claude"],gateway=lease,
                   connection_env={"C4G_BRIDGE_URL":"http://127.0.0.1:51002","C4G_BRIDGE_TOKEN":"y"*32})
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"]=="x"*32
    assert captured["env"]["HOME"]=="/home/c4gprobe/run"
    assert captured["output_mode"]=="stream"
    assert captured["env"]["API_TIMEOUT_MS"] == "1860000"
    assert captured["env"]["CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS"] == "1800000"
    assert captured["env"]["CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS"] == "1800000"
    assert captured["env"]["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "1800000"
    assert captured["env"]["BUN_CONFIG_HTTP_IDLE_TIMEOUT"] == "1800"
    with pytest.raises(ValueError, match="unsupported connection environment"):
        s.launch_agent("claude",["/opt/c4g-tools/claude"],gateway=lease,
                       connection_env={"API_TIMEOUT_MS":"0"})
    with pytest.raises(ValueError, match="unsupported connection environment"):
        s.launch_agent("claude",["/opt/c4g-tools/claude"],gateway=lease,
                       connection_env={"BUN_CONFIG_HTTP_IDLE_TIMEOUT":"0"})
    with pytest.raises(ValueError): s.launch_agent("codex",["/bin/codex"],gateway=lease)


@pytest.mark.parametrize("flag", ["_dependency_preparing", "_dependency_failed"])
def test_managed_cli_cannot_bypass_dependency_gate(monkeypatch, flag):
    s=bare(); setattr(s,flag,True)
    monkeypatch.setattr(s,"_launch",lambda *a,**k:pytest.fail("must not launch"))
    with pytest.raises(WslTransportError,match="has not sealed"):
        s.launch_agent("claude",["/opt/c4g-tools/claude"],gateway={})


def test_configured_codex_cli_uses_streamed_output(monkeypatch):
    s=bare(); captured={}
    monkeypatch.setattr(s.runtime.profile,"agent",lambda _n:PurePosixPath("/opt/c4g-tools/codex"))
    monkeypatch.setattr(s,"_launch",lambda argv,**kw:captured.update(argv=argv,**kw) or {"handle":"h"})
    lease={"url":"http://127.0.0.1:51005/v1","token":"x"*32,"port":51005}
    s.launch_codex(["/opt/c4g-tools/codex"],gateway=lease)
    assert captured["output_mode"]=="stream"
    assert captured["env"]["C4G_GATEWAY_TOKEN"]=="x"*32
    assert "API_TIMEOUT_MS" not in captured["env"]
    assert "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS" not in captured["env"]
    assert "BUN_CONFIG_HTTP_IDLE_TIMEOUT" not in captured["env"]


def test_final_test_cannot_select_streamed_cli_output(monkeypatch):
    s=bare("final_test")
    monkeypatch.setattr(s,"call",lambda *a,**k:pytest.fail("must not launch"))
    with pytest.raises(ValueError,match="development-only"):
        s._launch(["/usr/bin/python3"],output_mode="stream")


def test_launch_requires_child_in_role_cgroup(monkeypatch):
    s=bare(); s.distro="run"
    s.runtime._cgroups={"development":{"cgroup":"/sys/fs/cgroup/c4g123","uid":1000}}
    calls=[]
    class Native:
        def verify_child(self,*args): calls.append(("verify",args))
        def assign_child_cgroup(self,name,isolation,handle,pid):
            calls.append(("assign",(name,pid))); return isolation["cgroup"]+"/child-"+handle
    s.runtime.native=Native()
    def call(op,args,timeout=60):
        if op=="launch":
            assert args["output_mode"]=="bounded"
            return {"handle":"h","pid":42,"suspended":True}
        if op=="resume": return {"resumed":True}
        raise AssertionError(op)
    monkeypatch.setattr(s,"call",call)
    result=s.launch(["/usr/bin/python3","-V"])
    assert result["suspended"] is False
    assert calls==[
        ("verify",("run",42,1000,"/sys/fs/cgroup/c4g123","mnt:[2]")),
        ("assign",("run",42)),
        ("verify",("run",42,1000,"/sys/fs/cgroup/c4g123/child-h","mnt:[2]")),
    ]


def test_foreign_pid_is_rejected_before_privileged_cgroup_assignment(monkeypatch):
    s=bare(); s.distro="run"; s.runtime._cgroups={"development":{"cgroup":"/sys/fs/cgroup/base","uid":1000}}
    assigned=[]
    class Native:
        def verify_child(self,*args): raise RuntimeError("wrong uid")
        def assign_child_cgroup(self,*args): assigned.append(args)
    s.runtime.native=Native()
    monkeypatch.setattr(s,"call",lambda op,args=None,timeout=60: ({"handle":"h","pid":4} if op=="launch" else {"terminated":True}))
    monkeypatch.setattr(s,"close",lambda:None)
    with pytest.raises(RuntimeError,match="wrong uid"): s.launch(["/usr/bin/python3"])
    assert assigned==[]


def test_native_child_verification_rejects_wrong_cgroup(monkeypatch):
    native=WslNative()
    reply=type("Reply",(),{"stdout":b"1000 42 42\n0::/other\nmnt:[2]\n"})()
    monkeypatch.setattr(native,"exec",lambda *a,**k:reply)
    with pytest.raises(RuntimeError,match="escaped"):
        native.verify_child("run",42,1000,"/sys/fs/cgroup/c4g123")
    reply.stdout=b"1000 42 42\n0::/c4g123\nmnt:[2]\n"
    assert native.verify_child("run",42,1000,"/sys/fs/cgroup/c4g123","mnt:[2]")["cgroup"]=="/c4g123"


def test_mount_isolation_hello_requires_private_namespace_hidden_desktop_and_devices():
    hello={"mount_namespace":"mnt:[2]",
           "desktop_entrypoints":{"/dev/dxg":False,"/mnt/wslg":False,"/tmp/.X11-unix":False},
           "essential_devices":{"null":True,"pts":True,"random":True,"shm":True,"urandom":True,"zero":True},
           "resolver":{"available":True,"target":"/mnt/wsl/resolv.conf",
                       "scope":"private_recreated","mode":"0o444"}}
    assert _mount_isolation_from_hello(hello,"mnt:[1]")["private_mount_namespace"] is True
    hello["desktop_entrypoints"]["/dev/dxg"]=True
    with pytest.raises(Exception,match="mount isolation"): _mount_isolation_from_hello(hello,"mnt:[1]")


def test_private_mount_script_replaces_only_namespaced_integration_surfaces():
    script=_ISOLATE.decode()
    assert "mount --make-rprivate /" in script
    assert "tmpfs /tmp" in script and "tmpfs /mnt" in script and "tmpfs /run" in script and "tmpfs /dev" in script
    assert "mknod -m 0666 /dev/null" in script and "devpts /dev/pts" in script
    assert "umount" not in script and "wsl.conf" not in script
    assert script.index("mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /mnt") < script.index('cat <&9 > "$resolv_target"')


@pytest.mark.parametrize(("target","scope"),[
    ("/mnt/wsl/resolv.conf","private_recreated"),("/run/resolvconf/resolv.conf","private_recreated"),
    ("/tmp/generated-resolv.conf","private_recreated"),("/etc/resolv-static.conf","unmasked_retained"),
    ("/var/run-independent/resolv.conf","unmasked_retained")])
def test_resolver_target_scope_accepts_only_bounded_file_locations(target,scope):
    assert _resolver_target_scope(target)==scope


@pytest.mark.parametrize("target",["relative","/home/user/resolv.conf","/mnt/../etc/passwd","/"])
def test_resolver_target_scope_fails_closed_for_unsafe_or_unexpected_targets(target):
    with pytest.raises(Exception,match="resolver target"): _resolver_target_scope(target)


def test_supervisor_launcher_keeps_setsid_parent_waiting():
    source=Path(WslSupervisor.__init__.__code__.co_filename).read_text(encoding="utf-8")
    assert '"setsid","--wait","/usr/bin/unshare"' in source


def test_handshake_diagnostic_source_never_formats_token_value():
    secret="do-not-report-this-bearer"
    message=_handshake_error({"token":"wrong-secret","uid":999},secret,1000)
    assert "token_present=True" in message and "token_matches=False" in message
    assert "expected_uid=1000" in message and "observed_uid=999" in message
    assert secret not in message and "wrong-secret" not in message
    assert _handshake_error({"token":secret,"uid":1000},secret,1000) is None


def test_development_network_policy_scopes_ipv4_and_ipv6_to_run_cgroup(monkeypatch):
    native=WslNative(); seen={}
    monkeypatch.setattr(native,"uid_available",lambda *a:True)
    monkeypatch.setattr(native,"exec",lambda name,argv,**kwargs:seen.update(script=argv[-1]) or None)
    native.configure("C4G-WSL2-Run-id-development-abcdef",role="development",
                     control_port=51001,game_port=51002,gateway_port=51005)
    assert "meta skuid $run_uid" in seen["script"]
    assert "ip daddr 127.0.0.0/8" in seen["script"]
    assert "ip6 daddr ::1" in seen["script"]
    assert "ip6 daddr { ::1, fc00::/7, fe80::/10 } drop" in seen["script"]
