import importlib.util
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
from contextlib import contextmanager
from pathlib import PurePosixPath

import pytest


SCRIPT=Path(__file__).resolve().parents[2]/"scripts"/"acceptance"/"wsl2_runtime_canaries.py"
SPEC=importlib.util.spec_from_file_location("wsl2_runtime_canaries",SCRIPT)
MODULE=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


def observations(*, public, agent_executable=False, private_connected=False):
    return {
        "identity":{"uid":23456,"euid":23456,"groups":[23456],"root":False},
        "file_interop":{"mnt_c_mount":False,"host_sentinel_visible":False,"host_sentinel_readable":False,
                        "cmd_exe_visible":False,"manual_drvfs_mount_succeeded":False},
        "environment":{"model_names_present":[],"desktop_names_present":[]},
        "desktop_entrypoints":{"/tmp/.X11-unix":False,"/mnt/wslg":False,"/dev/dxg":False},
        "model_entrypoints":{"commands_present":{},"configured_agent_exists":True,
                             "configured_agent_executable":agent_executable},
        "functional":{"bash":True,"python_cv2_resize":True,"mcp_stdio_initialize_and_list":True,
                      "claude_version_exit_zero":True if public else None,"claude_version":"2.test" if public else None},
        "network":{"allowed_loopback_live_listener":True,"wrong_loopback_live_listener":False,
                   "allowed_ipv6_loopback_live_listener":True,"wrong_ipv6_loopback_live_listener":False,
                   "public_dns_resolved":public,"public_endpoint_connected":public,
                   "private_192_168_255_254_port_9_connected":False,
                   "controlled_private_ipv4_connected":private_connected,
                   "clone_local_private_ipv4_connected":None,
                   "clone_local_assigned_ipv6_connected":{},
                   "controlled_public_ipv6_connected":None,"controlled_public_ipv6_tls":None,
                   "controlled_private_ipv6_connected":None},
        "own_process":{"signal_zero":True,"same_uid":True,"separate_pgid":True},
    }


def test_role_policy_distinguishes_development_and_final_network_and_agent():
    assert MODULE._checks_pass(MODULE.assess("development",observations(public=True,agent_executable=True),23456))
    assert MODULE._checks_pass(MODULE.assess("final_test",observations(public=False),23456))


def test_assessment_fails_closed_for_host_file_and_model_environment():
    value=observations(public=False); value["file_interop"]["host_sentinel_readable"]=True
    value["environment"]["model_names_present"]=["OPENAI_API_KEY"]
    checks={row["name"]:row["passed"] for row in MODULE.assess("final_test",value,23456)}
    assert checks["windows_file_and_interop_entrypoints_absent"] is False
    assert checks["model_environment_absent"] is False


def test_probe_source_reports_no_environment_values_or_sentinel_contents():
    source=MODULE._probe_source()
    assert "os.environ.values" not in source
    assert "source.read()" not in source
    assert "model_names_present" in source
    assert 'connect("::1"' not in source
    assert 'cfg["ipv6_observed"]' in source
    assert 'struct.pack("ii",1,0)' in source
    assert 'connect(("::1",port))' in MODULE._ipv6_probe_source()
    assert 'struct.pack("ii",1,0)' in MODULE._ipv6_probe_source()
    assert "socket.AF_INET6" in source and "ssl.create_default_context()" in source
    assert "server_hostname=target" in source
    tcp_mark=source.index('result["tcp_connected"]=True')
    tls_wrap=source.index("ssl.create_default_context().wrap_socket")
    assert tcp_mark < tls_wrap


def test_listener_is_live_for_control_connection_and_releases_port():
    import socket
    with MODULE._listener(0) as port:
        assert port > 0
        with socket.create_connection(("127.0.0.1",port),1): pass
    replacement=socket.socket()
    try:
        replacement.bind(("127.0.0.1",port))
    finally:
        replacement.close()


def test_ephemeral_control_listener_excludes_profile_ports():
    import socket
    reserved=socket.socket(); reserved.bind(("127.0.0.1",0)); excluded=reserved.getsockname()[1]; reserved.close()
    with MODULE._listener(0,excluded_ports={excluded}) as port:
        assert port != excluded


def test_ipv6_listener_is_v6only_and_excludes_profile_ports(monkeypatch):
    calls=[]
    class FakeSocket:
        def setsockopt(self,*args): calls.append(("setsockopt",args))
        def bind(self,address): self.address=(address[0],54321)
        def getsockname(self): return self.address
        def listen(self,count): pass
        def settimeout(self,value): pass
        def accept(self): raise OSError()
        def close(self): pass
    monkeypatch.setattr(MODULE.socket,"socket",lambda family,kind: FakeSocket())
    with MODULE._listener(0,family=MODULE.socket.AF_INET6,excluded_ports={51002}) as port:
        assert port==54321
    assert ("setsockopt",(MODULE.socket.IPPROTO_IPV6,MODULE.socket.IPV6_V6ONLY,1)) in calls


@pytest.mark.parametrize("windows,fmt",[(True,"HH"),(False,"ii")])
def test_abortive_close_uses_platform_correct_linger_layout(windows,fmt):
    calls=[]
    class Connection:
        def setsockopt(self,*args): calls.append(args)
        def close(self): calls.append(("close",))
    MODULE._abortive_close(Connection(),windows=windows)
    assert calls[0][:2]==(socket.SOL_SOCKET,socket.SO_LINGER)
    assert struct.unpack(fmt,calls[0][2])==(1,0)
    assert calls[1]==("close",)


@pytest.mark.parametrize("number",[
    MODULE.errno.ECONNABORTED,MODULE.errno.ECONNRESET,MODULE.errno.ETIMEDOUT,10053,10054,10060])
def test_only_expected_reset_close_accept_errors_are_retryable(number):
    assert MODULE._expected_aborted_accept(OSError(number,"owned reset")) is True
    assert MODULE._expected_aborted_accept(OSError(MODULE.errno.EACCES,"unexpected")) is False


def test_fixed_listener_retries_only_owned_port_bind_and_records_readiness(monkeypatch):
    sockets=[]
    class FakeSocket:
        def __init__(self): self.failed=len(sockets)==0; sockets.append(self)
        def setsockopt(self,*args): pass
        def bind(self,address):
            if self.failed:
                error=OSError("address in use"); error.winerror=10048; raise error
            self.address=address
        def getsockname(self): return self.address
        def listen(self,count): pass
        def settimeout(self,value): pass
        def accept(self): raise OSError()
        def close(self): pass
    monkeypatch.setattr(MODULE.socket,"socket",lambda *args:FakeSocket())
    monkeypatch.setattr(MODULE.time,"sleep",lambda value:None)
    readiness={}
    with MODULE._listener(3437,bind_timeout=1,readiness=readiness) as port:
        assert port==3437
    assert readiness["ready"] is True and readiness["attempts"]==2
    assert readiness["fixed_port"] is True and readiness["port"]==3437


def test_fixed_listener_retries_windows_10013_then_succeeds(monkeypatch):
    attempts=[]
    class FakeSocket:
        def setsockopt(self,*args): pass
        def bind(self,address):
            attempts.append(address)
            if len(attempts)==1:
                error=PermissionError("mirrored reservation"); error.winerror=10013; raise error
            self.address=address
        def getsockname(self): return self.address
        def listen(self,count): pass
        def settimeout(self,value): pass
        def accept(self): raise OSError()
        def close(self): pass
    monkeypatch.setattr(MODULE.socket,"socket",lambda *args:FakeSocket())
    monkeypatch.setattr(MODULE.time,"sleep",lambda value:None)
    with MODULE._listener(3438,bind_timeout=10) as port: assert port==3438
    assert len(attempts)==2


def test_fixed_listener_persistent_windows_10013_expires_bounded(monkeypatch):
    clock=[0.0]
    class FakeSocket:
        def setsockopt(self,*args): pass
        def bind(self,address):
            error=PermissionError("mirrored reservation"); error.winerror=10013; raise error
        def close(self): pass
    monkeypatch.setattr(MODULE.socket,"socket",lambda *args:FakeSocket())
    monkeypatch.setattr(MODULE.time,"monotonic",lambda:clock[0])
    monkeypatch.setattr(MODULE.time,"sleep",lambda value:clock.__setitem__(0,clock[0]+value))
    with pytest.raises(PermissionError,match="mirrored reservation"):
        with MODULE._listener(3438,bind_timeout=.25): pass
    assert .25 <= clock[0] < .5


def test_fixed_listener_does_not_retry_generic_permission_error(monkeypatch):
    attempts=[]
    class FakeSocket:
        def setsockopt(self,*args): pass
        def bind(self,address): attempts.append(address); raise PermissionError(MODULE.errno.EACCES,"denied")
        def close(self): pass
    monkeypatch.setattr(MODULE.socket,"socket",lambda *args:FakeSocket())
    monkeypatch.setattr(MODULE.time,"sleep",lambda value:pytest.fail("generic EACCES must fail immediately"))
    with pytest.raises(PermissionError):
        with MODULE._listener(3438,bind_timeout=10): pass
    assert len(attempts)==1


def test_network_matrix_denial_and_missing_control_are_not_conflated():
    controlled={"private_ipv4":{"available":True,"host":"172.20.0.1","port":54321}}
    checks={row["name"]:row for row in MODULE.assess(
        "final_test",observations(public=False),23456,controls=controlled)}
    assert checks["controlled_private_ipv4_denied"]["passed"] is True
    assert checks["controlled_private_ipv4_denied"]["claimed"] is True
    leaked=observations(public=False,private_connected=True)
    checks={row["name"]:row for row in MODULE.assess("final_test",leaked,23456,controls=controlled)}
    assert checks["controlled_private_ipv4_denied"]["passed"] is False
    missing={row["name"]:row for row in MODULE.assess("final_test",observations(public=False),23456)}
    assert missing["controlled_private_ipv4_denied"]["passed"] is None
    assert missing["controlled_private_ipv4_denied"]["claimed"] is False
    assert missing["clone_local_private_ipv4_denied"]["passed"] is None
    assert missing["clone_local_private_ipv4_denied"]["claimed"] is False
    assert missing["controlled_public_ipv6_policy"]["passed"] is None
    assert missing["controlled_public_ipv6_policy"]["claimed"] is False
    assert missing["controlled_private_ipv6_denied"]["passed"] is None
    assert missing["controlled_private_ipv6_denied"]["claimed"] is False
    assert missing["clone_local_assigned_public_ipv6_policy"]["passed"] is None
    assert missing["clone_local_assigned_private_ipv6_denied"]["claimed"] is False
    assert MODULE._checks_pass(list(missing.values())) is True


@pytest.mark.parametrize("role,public",[("development",True),("final_test",False)])
def test_ipv6_loopback_role_cases_preserve_public_policy(role,public):
    checks={row["name"]:row["passed"] for row in MODULE.assess(role,observations(public=public),23456)}
    assert checks["allowed_ipv6_loopback_live_listener_reachable"] is True
    assert checks["wrong_ipv6_loopback_live_listener_denied"] is True
    value=observations(public=public); value["network"]["wrong_ipv6_loopback_live_listener"]=True
    failed={row["name"]:row["passed"] for row in MODULE.assess(role,value,23456)}
    assert failed["wrong_ipv6_loopback_live_listener_denied"] is False


def test_private_ipv4_control_parses_clone_route_and_requires_root_connection():
    class Native:
        def exec(self,distro,argv,**kwargs):
            assert distro=="fresh" and kwargs["user"]=="root" and kwargs["timeout"]==5
            assert argv[-2:]==["172.20.0.1","54321"]
            return type("Done",(),{"stdout":b'{"available": true, "host": "172.20.0.1", "port": 54321, "root_connected": true}'})()
    assert MODULE._private_ipv4_route_control(Native(),"fresh","172.20.0.1",54321)["available"] is True


def test_private_ipv4_control_rejects_nonprivate_or_failed_positive_control():
    class Native:
        def __init__(self,payload): self.payload=payload
        def exec(self,*args,**kwargs): return type("Done",(),{"stdout":self.payload})()
    public=b'{"available": true, "host": "1.1.1.1", "port": 54321, "root_connected": true}'
    failed=b'{"available": false, "reason": "TimeoutError"}'
    assert MODULE._private_ipv4_route_control(Native(public),"fresh","1.1.1.1",54321)["available"] is False
    assert MODULE._private_ipv4_route_control(Native(failed),"fresh","172.20.0.1",54321)=={
        "available":False,"reason":"TimeoutError"}


def test_windows_private_ipv4_uses_route_selected_source_without_sending(monkeypatch):
    calls=[]
    class Datagram:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def connect(self,target): calls.append(("connect",target))
        def getsockname(self): return ("192.168.50.10",40000)
        def send(self,*args): pytest.fail("route selection must not send a datagram")
    monkeypatch.setattr(MODULE.socket,"socket",lambda family,kind: Datagram())
    value=MODULE._windows_private_ipv4("151.101.0.223")
    assert value["available"] is True and value["host"]=="192.168.50.10"
    assert calls==[("connect",("151.101.0.223",443))]


def test_public_ipv6_candidates_are_global_nonmapped_bounded_and_deduplicated(monkeypatch):
    records=[
        (socket.AF_INET6,socket.SOCK_STREAM,0,"",("::1",443,0,0)),
        (socket.AF_INET6,socket.SOCK_STREAM,0,"",("::ffff:192.0.2.1",443,0,0)),
        (socket.AF_INET6,socket.SOCK_STREAM,0,"",("240c::1",443,0,0)),
        (socket.AF_INET6,socket.SOCK_STREAM,0,"",("240c:0:0:0:0:0:0:1",443,0,0)),
        (socket.AF_INET6,socket.SOCK_STREAM,0,"",("2606::2",443,0,0))]
    monkeypatch.setattr(MODULE.socket,"getaddrinfo",lambda *args:records)
    value=MODULE._public_ipv6_candidates(limit=2)
    assert value["available"] is True
    assert value["addresses"]==["240c::1","2606::2"]


def test_clone_public_ipv6_control_requires_exact_direct_certificate_validated_tls():
    calls=[]
    class Native:
        def exec(self,distro,argv,**kwargs):
            calls.append((distro,argv,kwargs))
            result={"available":True,"host":"240c::1","port":443,"server_hostname":"pypi.org",
                    "tcp_tls":True,"certificate_validated":True,"tls_version":"TLSv1.3",
                    "transport":"direct_AF_INET6_TLS_no_HTTP_no_proxy"}
            return type("Done",(),{"stdout":json.dumps(result).encode()})()
    candidates={"available":True,"host":"pypi.org","port":443,"addresses":["240c::1"]}
    value=MODULE._clone_public_ipv6_tls_control(Native(),"fresh",candidates)
    assert value["available"] is True and value["certificate_validated"] is True
    _,argv,kwargs=calls[0]
    assert kwargs["user"]=="root" and kwargs["timeout"]==9
    assert json.loads(kwargs["input"])=={"host":"pypi.org","port":443,"addresses":["240c::1"]}
    source=argv[-1]
    assert "socket.AF_INET6" in source and "ssl.create_default_context()" in source
    assert "server_hostname=cfg" in source and "time.monotonic()+7.5" in source
    assert "signal.setitimer(signal.ITIMER_REAL,7.75)" in source
    assert "requests" not in source and "urllib" not in source


@pytest.mark.parametrize("role,connected",[("development",True),("final_test",False)])
def test_external_public_ipv6_role_policy_uses_fresh_clone_root_control(role,connected):
    control={"available":True,"host":"240c::1","port":443,"server_hostname":"pypi.org",
             "tcp_tls":True,"certificate_validated":True,"tls_version":"TLSv1.3",
             "transport":"direct_AF_INET6_TLS_no_HTTP_no_proxy"}
    value=observations(public=role=="development")
    value["network"]["controlled_public_ipv6_connected"]=connected
    value["network"]["controlled_public_ipv6_tls"]={
        "tcp_connected":connected,"tls_validated":connected if role=="development" else False,
        "certificate_validated":connected if role=="development" else False,
        "tls_version":"TLSv1.3" if connected and role=="development" else None,
        "error_type":None if connected and role=="development" else "TimeoutError"}
    checks={row["name"]:row for row in MODULE.assess(role,value,23456,controls={"public_ipv6":control})}
    assert checks["controlled_public_ipv6_policy"]["passed"] is True
    value["network"]["controlled_public_ipv6_connected"]=not connected
    value["network"]["controlled_public_ipv6_tls"]["tcp_connected"]=not connected
    failed={row["name"]:row for row in MODULE.assess(role,value,23456,controls={"public_ipv6":control})}
    assert failed["controlled_public_ipv6_policy"]["passed"] is False


@pytest.mark.parametrize("role,tcp,tls,expected",[
    ("final_test",True,False,False),
    ("development",True,False,False),
    ("final_test",False,False,True),
    ("development",True,True,True),
])
def test_external_public_ipv6_tcp_and_tls_semantics(role,tcp,tls,expected):
    control={"available":True,"host":"240c::1","port":443,"server_hostname":"pypi.org",
             "tcp_tls":True,"certificate_validated":True,"tls_version":"TLSv1.3",
             "transport":"direct_AF_INET6_TLS_no_HTTP_no_proxy"}
    value=observations(public=role=="development")
    value["network"]["controlled_public_ipv6_connected"]=tcp
    value["network"]["controlled_public_ipv6_tls"]={
        "tcp_connected":tcp,"tls_validated":tls,"certificate_validated":tls,
        "tls_version":"TLSv1.3" if tls else None,"error_type":None if tls else "SSLError"}
    check={row["name"]:row for row in MODULE.assess(role,value,23456,controls={"public_ipv6":control})}[
        "controlled_public_ipv6_policy"]
    assert check["passed"] is expected


def test_external_public_ipv6_without_fresh_clone_root_route_is_unclaimed():
    value=observations(public=True)
    check={row["name"]:row for row in MODULE.assess(
        "development",value,23456,controls={"public_ipv6":{"available":False,"reason":"TimeoutError"}})}[
            "controlled_public_ipv6_policy"]
    assert check["passed"] is None and check["claimed"] is False and check["reason"]=="TimeoutError"


def test_wsl_root_ipv6_listener_is_exact_clone_root_v6only_and_bounded(monkeypatch):
    ready={"ready":True,"allowed_port":51002,"wrong_port":54321,
           "root_connected":True,"wrong_root_connected":True,"v6only":True,"pid":321}
    calls=[]
    class Process:
        def __init__(self):
            self.stdin=io.BytesIO(); self.stdout=io.BytesIO((json.dumps(ready)+"\n").encode())
            self.stderr=io.BytesIO(); self.returncode=None
        def wait(self,timeout): calls.append(("wait",timeout)); self.returncode=0; return 0
    process=Process()
    def popen(command,**kwargs): calls.append(("popen",command,kwargs)); return process
    monkeypatch.setattr(MODULE.subprocess,"Popen",popen)
    native=type("Native",(),{"executable":"wsl.exe"})()
    with MODULE._wsl_root_ipv6_listener(native,"exact-fresh-clone",51002,{51001,51003}) as value:
        assert value==ready
    command=calls[0][1]
    assert command[:7]==["wsl.exe","--distribution","exact-fresh-clone","--user","root","--exec","/usr/bin/python3"]
    source=command[11]
    assert "socket.AF_INET6" in source and "socket.IPV6_V6ONLY" in source
    assert "wrong.listen" in source and "wrong_root_connected" in source
    assert 'struct.pack("ii",1,0)' in source
    self_control=source.index('client=socket.create_connection(("::1",port),2)')
    accepted=source.index("connection,_=listener.accept()",self_control)
    reset_closed=source.index("client.setsockopt",self_control)
    ready=source.index('print(json.dumps({"ready":True')
    assert self_control < accepted < reset_closed < ready
    assert "errno.ECONNABORTED" in source and "errno.ECONNRESET" in source
    assert calls[-1]==("wait",20)


def test_wsl_root_listener_timeout_never_kills_unverified_process(monkeypatch):
    ready={"ready":True,"allowed_port":51002,"wrong_port":54321,
           "root_connected":True,"wrong_root_connected":True,"v6only":True,"pid":321}
    class Process:
        stdin=io.BytesIO(); stdout=io.BytesIO((json.dumps(ready)+"\n").encode()); stderr=io.BytesIO()
        returncode=None
        def wait(self,timeout): raise MODULE.subprocess.TimeoutExpired("wsl",timeout)
        def kill(self): pytest.fail("must not kill a process without post-handshake identity verification")
        def terminate(self): pytest.fail("must not terminate a process without post-handshake identity verification")
    monkeypatch.setattr(MODULE.subprocess,"Popen",lambda *args,**kwargs:Process())
    native=type("Native",(),{"executable":"wsl.exe"})()
    with pytest.raises(MODULE.RootListenerCleanupError,match="preserved"):
        with MODULE._wsl_root_ipv6_listener(native,"exact-fresh-clone",51002,{51001}): pass


def test_wsl_root_private_ipv4_listener_requires_assigned_rfc1918_and_root_positive(monkeypatch):
    ready={"available":True,"host":"172.20.1.2","port":51002,"root_connected":True,
           "pid":322,"address_source":"assigned_interface_ioctl"}
    calls=[]
    class Process:
        def __init__(self):
            self.stdin=io.BytesIO(); self.stdout=io.BytesIO((json.dumps(ready)+"\n").encode())
            self.stderr=io.BytesIO(); self.returncode=None
        def wait(self,timeout): calls.append(("wait",timeout)); self.returncode=0
    monkeypatch.setattr(MODULE.subprocess,"Popen",lambda command,**kwargs:(calls.append(("popen",command)) or Process()))
    native=type("Native",(),{"executable":"wsl.exe"})()
    with MODULE._wsl_root_private_ipv4_listener(native,"fresh",51002) as value:
        assert value==ready
    source=calls[0][1][11]
    assert "socket.if_nameindex()" in source and "fcntl.ioctl" in source
    assert "10.0.0.0/8" in source and "172.16.0.0/12" in source and "192.168.0.0/16" in source
    connected=source.index("client=socket.create_connection((address,port),2)")
    accepted=source.index("connection,_=server.accept()",connected)
    reset=source.index("client.setsockopt",connected)
    announced=source.index('print(json.dumps({"available":True')
    assert connected < accepted < reset < announced


def test_wsl_root_private_ipv4_listener_explicitly_skips_without_assigned_address(monkeypatch):
    ready={"available":False,"reason":"no_assigned_nonloopback_rfc1918_ipv4"}
    class Process:
        def __init__(self):
            self.stdin=io.BytesIO(); self.stdout=io.BytesIO((json.dumps(ready)+"\n").encode())
            self.stderr=io.BytesIO(); self.returncode=None
        def wait(self,timeout): self.returncode=0
    monkeypatch.setattr(MODULE.subprocess,"Popen",lambda *args,**kwargs:Process())
    native=type("Native",(),{"executable":"wsl.exe"})()
    with MODULE._wsl_root_private_ipv4_listener(native,"fresh",51002) as value:
        assert value==ready


def test_clone_private_ipv4_control_denial_and_leak_are_distinct(monkeypatch):
    control={"available":True,"host":"172.20.1.2","port":51002,"root_connected":True}
    @contextmanager
    def listener(*args,**kwargs): yield control
    monkeypatch.setattr(MODULE,"_wsl_root_private_ipv4_listener",listener)
    monkeypatch.setattr(MODULE,"_launch_and_collect",lambda *args,**kwargs:{"connected":False})
    supervisor=type("Supervisor",(),{"role":"development"})()
    actual_control,observed=MODULE._run_private_ipv4_phase(
        supervisor,object(),"fresh","/usr/bin/python3",PurePosixPath("/run/staged"),51002)
    assert actual_control is control and observed=={"connected":False}
    value=observations(public=True); value["network"]["clone_local_private_ipv4_connected"]=False
    checks={row["name"]:row for row in MODULE.assess(
        "development",value,23456,controls={"clone_private_ipv4":control})}
    assert checks["clone_local_private_ipv4_denied"]["passed"] is True
    value["network"]["clone_local_private_ipv4_connected"]=True
    checks={row["name"]:row for row in MODULE.assess(
        "development",value,23456,controls={"clone_private_ipv4":control})}
    assert checks["clone_local_private_ipv4_denied"]["passed"] is False


def test_clone_private_ipv4_missing_address_is_unclaimed_without_uid_probe(monkeypatch):
    unavailable={"available":False,"reason":"no_assigned_nonloopback_rfc1918_ipv4"}
    @contextmanager
    def listener(*args,**kwargs): yield unavailable
    monkeypatch.setattr(MODULE,"_wsl_root_private_ipv4_listener",listener)
    monkeypatch.setattr(MODULE,"_launch_and_collect",lambda *args,**kwargs:pytest.fail("no UID probe without listener"))
    supervisor=type("Supervisor",(),{"role":"final_test"})()
    control,observed=MODULE._run_private_ipv4_phase(
        supervisor,object(),"fresh","/usr/bin/python3",PurePosixPath("/run/staged"),51004)
    assert control==unavailable and observed is None


def assigned_ipv6_control():
    return {"targets":{
        "public":{"available":True,"host":"240c::123","port":51002,"scope_id":0,
                  "address_class":"global","root_connected":True},
        "private":{"available":True,"host":"fe80::123","port":51002,"scope_id":2,
                   "ifindex":2,"address_class":"link_local","root_connected":True}}}


@pytest.mark.parametrize("role,public_connected",[("development",True),("final_test",False)])
def test_clone_assigned_ipv6_role_policy_and_private_denial(role,public_connected):
    value=observations(public=role=="development")
    value["network"]["clone_local_assigned_ipv6_connected"]={
        "public":public_connected,"private":False}
    checks={row["name"]:row for row in MODULE.assess(
        role,value,23456,controls={"clone_assigned_ipv6":assigned_ipv6_control()})}
    assert checks["clone_local_assigned_public_ipv6_policy"]["passed"] is True
    assert checks["clone_local_assigned_private_ipv6_denied"]["passed"] is True
    value["network"]["clone_local_assigned_ipv6_connected"]["private"]=True
    leaked={row["name"]:row for row in MODULE.assess(
        role,value,23456,controls={"clone_assigned_ipv6":assigned_ipv6_control()})}
    assert leaked["clone_local_assigned_private_ipv6_denied"]["passed"] is False


def test_final_clone_assigned_public_ipv6_leak_fails():
    value=observations(public=False)
    value["network"]["clone_local_assigned_ipv6_connected"]={"public":True,"private":False}
    checks={row["name"]:row for row in MODULE.assess(
        "final_test",value,23456,controls={"clone_assigned_ipv6":assigned_ipv6_control()})}
    assert checks["clone_local_assigned_public_ipv6_policy"]["passed"] is False


def assigned_ipv6_ready():
    return {"targets":{
        "public":{"available":True,"host":"240c::123","port":51002,"scope_id":0,"ifindex":2,
                  "interface":"eth0","prefix_length":64,"kernel_scope":0,"flags":0x82,
                  "address_class":"global","address_source":"proc_net_if_inet6","root_connected":True},
        "private":{"available":True,"host":"fe80::123","port":51002,"scope_id":2,"ifindex":2,
                   "interface":"eth0","prefix_length":64,"kernel_scope":0x20,"flags":0x82,
                   "address_class":"link_local","address_source":"proc_net_if_inet6","root_connected":True}},
        "pid":323,"v6only":True,"address_source":"proc_net_if_inet6"}


def test_assigned_ipv6_listener_parser_flags_scope_root_controls_and_cleanup(monkeypatch):
    ready=assigned_ipv6_ready(); calls=[]
    class Process:
        def __init__(self):
            self.stdin=io.BytesIO(); self.stdout=io.BytesIO((json.dumps(ready)+"\n").encode())
            self.stderr=io.BytesIO(); self.returncode=None
        def wait(self,timeout): calls.append(("wait",timeout)); self.returncode=0
    monkeypatch.setattr(MODULE.subprocess,"Popen",lambda command,**kwargs:(calls.append(("popen",command)) or Process()))
    native=type("Native",(),{"executable":"wsl.exe"})()
    with MODULE._wsl_root_assigned_ipv6_listeners(native,"fresh",51002) as value:
        assert value==ready
    source=calls[0][1][11]
    assert 'open("/proc/net/if_inet6"' in source
    assert "flags & (0x40|0x08)" in source
    assert "scope_id=ifindex if address.is_link_local else 0" in source
    assert "socket.IPV6_V6ONLY" in source
    assert calls[-1]==("wait",20)


def test_assigned_ipv6_missing_classes_are_unclaimed_without_uid_probe(monkeypatch):
    unavailable={"targets":{
        "public":{"available":False,"reason":"no_qualified_assigned_public_ipv6"},
        "private":{"available":False,"reason":"no_qualified_assigned_private_ipv6"}},
        "pid":323,"v6only":True,"address_source":"proc_net_if_inet6"}
    @contextmanager
    def listeners(*args,**kwargs): yield unavailable
    monkeypatch.setattr(MODULE,"_wsl_root_assigned_ipv6_listeners",listeners)
    monkeypatch.setattr(MODULE,"_launch_and_collect",lambda *args,**kwargs:pytest.fail("no UID probe without targets"))
    supervisor=type("Supervisor",(),{"role":"development"})()
    control,observed=MODULE._run_assigned_ipv6_phase(
        supervisor,object(),"fresh","/usr/bin/python3",PurePosixPath("/run/staged"),51002)
    assert control==unavailable and observed=={}


def test_assigned_ipv6_cleanup_timeout_preserves_without_kill(monkeypatch):
    ready=assigned_ipv6_ready()
    class Process:
        def __init__(self):
            self.stdin=io.BytesIO(); self.stdout=io.BytesIO((json.dumps(ready)+"\n").encode())
            self.stderr=io.BytesIO(); self.returncode=None
        def wait(self,timeout): raise MODULE.subprocess.TimeoutExpired("wsl",timeout)
        def kill(self): pytest.fail("must not kill by reported PID")
        def terminate(self): pytest.fail("must not terminate by reported PID")
    monkeypatch.setattr(MODULE.subprocess,"Popen",lambda *args,**kwargs:Process())
    native=type("Native",(),{"executable":"wsl.exe"})()
    with pytest.raises(MODULE.RootListenerCleanupError,match="preserved"):
        with MODULE._wsl_root_assigned_ipv6_listeners(native,"fresh",51002): pass


def test_ipv6_phase_closes_root_helper_before_caller_starts_windows_phase(monkeypatch):
    events=[]
    @contextmanager
    def root_listener(native,distro,allowed_port,excluded_ports):
        events.append("root_enter")
        yield {"ready":True,"allowed_port":allowed_port,"wrong_port":54321,
               "root_connected":True,"wrong_root_connected":True,"v6only":True,"pid":321}
        events.append("root_exit")
    def launch(supervisor,argv,**kwargs):
        events.append("uid_probe")
        assert json.loads(kwargs["stdin_text"])=={"allowed_port":51002,"wrong_port":54321}
        return {"allowed_connected":True,"wrong_connected":False}
    monkeypatch.setattr(MODULE,"_wsl_root_ipv6_listener",root_listener)
    monkeypatch.setattr(MODULE,"_launch_and_collect",launch)
    supervisor=type("Supervisor",(),{"role":"development"})()
    control,observed=MODULE._run_ipv6_phase(
        supervisor,object(),"fresh","/usr/bin/python3",PurePosixPath("/run/staged"),51002,{51001})
    events.append("windows_bind")
    assert events==["root_enter","uid_probe","root_exit","windows_bind"]
    assert observed=={"allowed_connected":True,"wrong_connected":False}
    assert control["root_connected"] is True and control["wrong_root_connected"] is True


def test_ipv6_phase_rejects_missing_or_nonboolean_observations(monkeypatch):
    @contextmanager
    def root_listener(*args,**kwargs): yield {"wrong_port":54321}
    monkeypatch.setattr(MODULE,"_wsl_root_ipv6_listener",root_listener)
    monkeypatch.setattr(MODULE,"_launch_and_collect",lambda *args,**kwargs:{
        "allowed_connected":1,"wrong_connected":False})
    supervisor=type("Supervisor",(),{"role":"final_test"})()
    with pytest.raises(RuntimeError,match="invalid IPv6"):
        MODULE._run_ipv6_phase(
            supervisor,object(),"fresh","/usr/bin/python3",PurePosixPath("/run/staged"),51004,{51001})


def test_launch_and_collect_polls_handle_from_supervisor_launch_record():
    class Supervisor:
        def __init__(self): self.polled=[]
        def launch(self,argv,**kwargs):
            assert argv==["python","probe.py"]
            assert kwargs["stdin_text"]=="{}"
            return {"handle":"child-handle","pid":123,"running":True}
        def poll(self,handle):
            self.polled.append(handle)
            return {"running":False,"returncode":0,"output_complete":True,
                    "events":[{"stream":"stdout","data":"{\"passed\": true}","partial":False}]}
    supervisor=Supervisor()
    assert MODULE._launch_and_collect(supervisor,["python","probe.py"],cwd="/run",env={},stdin_text="{}") == {"passed":True}
    assert supervisor.polled==["child-handle"]


def test_generated_guest_source_runs_with_standard_python_and_own_child(tmp_path):
    sentinel=tmp_path/"sentinel"; sentinel.write_bytes(b"harmless")
    with MODULE._listener(0) as allowed, MODULE._listener(0,excluded_ports={allowed}) as wrong:
        cfg={"host_sentinel":str(sentinel),"workspace":str(tmp_path),"allowed_port":allowed,"wrong_port":wrong,
             "ipv6_observed":{"allowed_connected":True,"wrong_connected":False},"private_ipv4_target":None,
             "clone_private_ipv4_observed":None,
             "clone_assigned_ipv6_observed":{},
             "public_ipv6_target":None,"private_ipv6_target":None,
             "model_env_names":[],"agent_path":str(tmp_path/"missing-agent"),"local_validation":True,
             "role":"final_test","mcp_client":str(MODULE.REPO/"coding4game_gui"/"runtime_client"/"client.py"),
             "public_host":"localhost","public_ipv4":"127.0.0.1"}
        process=subprocess.run([sys.executable,"-c",MODULE._probe_source()],input=json.dumps(cfg),text=True,
                               capture_output=True,timeout=10)
    assert process.returncode==0,process.stderr
    result=json.loads(process.stdout)
    assert result["own_process"]["signal_zero"] is True
    assert result["network"]["allowed_loopback_live_listener"] is True
    assert result["functional"]["mcp_stdio_initialize_and_list"] is True


def test_busy_profile_refusal_leaves_no_evidence_directory(tmp_path):
    class Profile:
        id="0123456789ab"; baseline_sha256="0"*64
    class BusyRuntime:
        def __init__(self, profile, evidence_dir): pass
        def __enter__(self): raise RuntimeError("WSL2 profile already in use")
        def __exit__(self, *args): pass
    out=tmp_path/"evidence"
    with pytest.raises(RuntimeError,match="already in use"):
        MODULE.execute(Profile(),out,runtime_factory=BusyRuntime)
    assert not out.exists()
