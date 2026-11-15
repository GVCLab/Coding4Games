"""No-model, no-game canaries for fresh WSL2 runtime clones.

Preview is the default.  Execution acquires the profile runtime lock, imports one
fresh clone per role, runs only harmless probes as the configured run UID, then
uses WslRuntime cleanup to export and unregister both clones.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import queue
from contextlib import ExitStack, contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from coding4game_gui.wsl2 import WslProfile, WslRuntime


SCHEMA = 1
MODEL_ENV_NAMES = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL",
}


class RootListenerCleanupError(RuntimeError): pass


def _windows_to_wsl(path: Path) -> str:
    value = path.resolve()
    if len(value.drive) != 2:
        raise ValueError("evidence directory must be on a Windows drive")
    return f"/mnt/{value.drive[0].lower()}/{value.as_posix()[2:].lstrip('/')}"


def _abortive_close(connection, *, windows: bool) -> None:
    connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,
                          struct.pack("HH" if windows else "ii",1,0))
    connection.close()


def _expected_aborted_accept(error: OSError) -> bool:
    return error.errno in {errno.ECONNABORTED,errno.ECONNRESET,errno.ETIMEDOUT,10053,10054,10060}


def _retryable_fixed_bind_error(error: OSError) -> bool:
    return error.errno == errno.EADDRINUSE or getattr(error,"winerror",None) in {10013,10048}


@contextmanager
def _listener(port: int, *, family=socket.AF_INET, excluded_ports=(), host=None,
              bind_timeout=0, readiness=None):
    stop = threading.Event()
    excluded_ports=set(excluded_ports)
    if port and port in excluded_ports:
        raise ValueError("fixed listener port is excluded")
    if family not in (socket.AF_INET,socket.AF_INET6): raise ValueError("unsupported listener family")
    bind_host=host if host is not None else ("127.0.0.1" if family == socket.AF_INET else "::1")
    deadline=time.monotonic()+bind_timeout; attempts=0; started=time.monotonic()
    while True:
        attempts+=1
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
        try: sock.bind((bind_host, port))
        except OSError as error:
            sock.close()
            if port and _retryable_fixed_bind_error(error) and time.monotonic()<deadline:
                time.sleep(.1); continue
            raise
        if sock.getsockname()[1] not in excluded_ports:
            break
        sock.close()
    if readiness is not None:
        readiness.update({"ready":True,"attempts":attempts,
                          "bind_wait_seconds":round(time.monotonic()-started,3),
                          "fixed_port":bool(port),"host":bind_host,"port":sock.getsockname()[1]})
    sock.listen(8); sock.settimeout(.1)

    def serve():
        while not stop.is_set():
            try:
                connection, _ = sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError as error:
                if not stop.is_set() and _expected_aborted_accept(error): continue
                break
            _abortive_close(connection,windows=True)

    thread = threading.Thread(target=serve, daemon=True); thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set(); sock.close(); thread.join(1)


def _probe_source() -> str:
    # This program reports names and booleans only. It never reports environment
    # values, sentinel contents, process command lines, or file contents.
    return r'''import json, os, pathlib, shutil, signal, socket, ssl, struct, subprocess, sys, time
cfg=json.load(sys.stdin)
def connect(host,port,abortive=False):
    try:
        client=socket.create_connection((host,port),2)
        if abortive: client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0))
        client.close(); return True
    except OSError: return False
def resolve(host):
    previous=os.environ.get("RES_OPTIONS")
    os.environ["RES_OPTIONS"]="attempts:1 timeout:1"
    try: return bool(socket.getaddrinfo(host,443,socket.AF_INET,socket.SOCK_STREAM))
    except OSError: return False
    finally:
        if previous is None: os.environ.pop("RES_OPTIONS",None)
        else: os.environ["RES_OPTIONS"]=previous
def tls_ipv6(target):
    result={"tcp_connected":False,"tls_validated":False,"certificate_validated":False,
            "tls_version":None,"error_type":None}
    raw=None
    try:
        raw=socket.socket(socket.AF_INET6,socket.SOCK_STREAM); raw.settimeout(4)
        raw.connect((target["host"],target["port"],0,0))
        result["tcp_connected"]=True
        with ssl.create_default_context().wrap_socket(raw,server_hostname=target["server_hostname"]) as tls:
            validated=bool(tls.getpeercert())
            result.update(tls_validated=validated,certificate_validated=validated,tls_version=tls.version())
            if not validated: result["error_type"]="CertificateUnavailable"
    except (OSError,ssl.SSLError) as error: result["error_type"]=type(error).__name__
    finally:
        if raw is not None:
            try: raw.close()
            except OSError: pass
    return result
def exists(path):
    try: return pathlib.Path(path).exists()
    except OSError: return False
def is_mount(path):
    try: return pathlib.Path(path).is_mount()
    except (OSError,NotImplementedError): return False
sentinel=pathlib.Path(cfg["host_sentinel"])
uid=getattr(os,"getuid",lambda:-1)(); euid=getattr(os,"geteuid",lambda:-1)()
sentinel_visible=exists(sentinel)
sentinel_readable=False
if sentinel_visible:
    try:
        with sentinel.open("rb") as source: sentinel_readable=bool(source.read(1))
    except OSError: pass
mountpoint=pathlib.Path(cfg["workspace"])/"drvfs-canary"; mountpoint.mkdir()
if sys.platform.startswith("linux") and pathlib.Path("/bin/mount").is_file():
    mount=subprocess.run(["/bin/mount","-t","drvfs","C:",str(mountpoint)],capture_output=True)
    if mount.returncode == 0: subprocess.run(["/bin/umount",str(mountpoint)],capture_output=True)
    mount_succeeded=mount.returncode == 0
else: mount_succeeded=False
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],start_new_session=True)
try:
    own_child={"signal_zero":False,"same_uid":False,"separate_pgid":False}
    os.kill(child.pid,0); own_child["signal_zero"]=True
    status_path=pathlib.Path(f"/proc/{child.pid}/status")
    if status_path.is_file():
        uid_line=next((line for line in status_path.read_text().splitlines() if line.startswith("Uid:")),"")
        fields=uid_line.split()
        own_child["same_uid"]=len(fields)>1 and int(fields[1])==uid
    if hasattr(os,"getpgid"): own_child["separate_pgid"]=os.getpgid(child.pid)==child.pid
finally:
    child.terminate()
    try: child.wait(2)
    except subprocess.TimeoutExpired: child.kill(); child.wait()
desktop_env=sorted(k for k in ("DISPLAY","WAYLAND_DISPLAY","WSL2_GUI_APPS_ENABLED","WSLG_RUNTIME_DIR") if k in os.environ)
desktop_paths={p:exists(p) for p in ("/tmp/.X11-unix","/mnt/wslg","/dev/dxg")}
model_env=sorted(k for k in cfg["model_env_names"] if k in os.environ)
model_commands={name:shutil.which(name) is not None for name in ("ollama","llama-server","llamafile","claude","codex","gemini")}
agent=pathlib.Path(cfg["agent_path"])
functional={"bash":False,"python_cv2_resize":False,"mcp_stdio_initialize_and_list":False,
            "claude_version_exit_zero":None,"claude_version":None}
try:
    bash=subprocess.run(["/bin/bash","-lc","test x = x"],capture_output=True,timeout=5)
    functional["bash"]=bash.returncode == 0
except (OSError,subprocess.SubprocessError): pass
try:
    import cv2,numpy
    resized=cv2.resize(numpy.zeros((2,3,3),dtype=numpy.uint8),(5,4))
    functional["python_cv2_resize"]=resized.shape == (4,5,3)
except Exception: pass
try:
    requests=(json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})+"\n"+
              json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})+"\n")
    mcp=subprocess.run([sys.executable,"-I","-B",cfg["mcp_client"],"--mcp"],input=requests,text=True,
                       capture_output=True,timeout=10,env={"PATH":"/usr/bin:/bin","C4G_BRIDGE_PHASE":cfg["role"]})
    replies=[json.loads(line) for line in mcp.stdout.splitlines() if line.strip()]
    by_id={reply.get("id"):reply for reply in replies}
    functional["mcp_stdio_initialize_and_list"]=(mcp.returncode == 0 and set(by_id) == {1,2}
        and isinstance(by_id[1].get("result",{}).get("serverInfo"),dict)
        and isinstance(by_id[2].get("result",{}).get("tools"),list) and bool(by_id[2]["result"]["tools"]))
except Exception: pass
if cfg["role"] == "development":
    try:
        version=subprocess.run([str(agent),"--version"],capture_output=True,text=True,timeout=10,
            env={"HOME":cfg["workspace"],"PATH":"/opt/c4g-tools:/usr/local/bin:/usr/bin:/bin","LANG":"C.UTF-8"})
        functional["claude_version_exit_zero"]=version.returncode == 0
        functional["claude_version"]=(version.stdout or version.stderr).strip().splitlines()[0][:200]
    except (OSError,subprocess.SubprocessError,IndexError): functional["claude_version_exit_zero"]=False
public_ipv4=False if cfg.get("local_validation") else connect(cfg["public_ipv4"],443)
private_target=cfg.get("private_ipv4_target")
private_ipv4=None if not private_target else connect(private_target["host"],private_target["port"],True)
public_ipv6_target=cfg.get("public_ipv6_target")
public_ipv6=None if not public_ipv6_target else tls_ipv6(public_ipv6_target)
private_ipv6_target=cfg.get("private_ipv6_target")
private_ipv6=None if not private_ipv6_target else connect(private_ipv6_target["host"],private_ipv6_target["port"])
dns_public=False if cfg.get("local_validation") else resolve(cfg["public_host"])
result={
 "identity":{"uid":uid,"euid":euid,"groups":getattr(os,"getgroups",lambda:[])(),"root":euid==0},
 "file_interop":{"mnt_c_mount":is_mount("/mnt/c"),"host_sentinel_visible":sentinel_visible,
   "host_sentinel_readable":sentinel_readable,"cmd_exe_visible":exists("/mnt/c/Windows/System32/cmd.exe"),
   "manual_drvfs_mount_succeeded":mount_succeeded},
 "environment":{"model_names_present":model_env,"desktop_names_present":desktop_env},
 "desktop_entrypoints":desktop_paths,
 "model_entrypoints":{"commands_present":model_commands,"configured_agent_exists":agent.exists(),
   "configured_agent_executable":os.access(agent,os.X_OK)},
 "functional":functional,
 "network":{"allowed_loopback_live_listener":connect("127.0.0.1",cfg["allowed_port"],True),
   "wrong_loopback_live_listener":connect("127.0.0.1",cfg["wrong_port"],True),
   "allowed_ipv6_loopback_live_listener":cfg["ipv6_observed"]["allowed_connected"],
   "wrong_ipv6_loopback_live_listener":cfg["ipv6_observed"]["wrong_connected"],
   "public_dns_resolved":dns_public,
   "public_endpoint_connected":public_ipv4,
   "private_192_168_255_254_port_9_connected":False if cfg.get("local_validation") else connect("192.168.255.254",9,True),
   "controlled_private_ipv4_connected":private_ipv4,
   "clone_local_private_ipv4_connected":cfg["clone_private_ipv4_observed"],
   "clone_local_assigned_ipv6_connected":cfg["clone_assigned_ipv6_observed"],
   "controlled_public_ipv6_connected":None if public_ipv6 is None else public_ipv6["tcp_connected"],
   "controlled_public_ipv6_tls":public_ipv6,
   "controlled_private_ipv6_connected":private_ipv6},
 "own_process":own_child,
}
print(json.dumps(result,sort_keys=True))
'''


def _ipv6_probe_source() -> str:
    return r'''import json,socket,struct,sys
cfg=json.load(sys.stdin)
def connect(port):
    try:
        with socket.socket(socket.AF_INET6,socket.SOCK_STREAM) as client:
            client.settimeout(2); client.connect(("::1",port))
            client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); return True
    except OSError: return False
print(json.dumps({"allowed_connected":connect(cfg["allowed_port"]),
                  "wrong_connected":connect(cfg["wrong_port"])}),flush=True)
'''


def _private_ipv4_probe_source() -> str:
    return r'''import json,socket,struct,sys
cfg=json.load(sys.stdin)
try:
    client=socket.create_connection((cfg["host"],cfg["port"]),2)
    client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
    connected=True
except OSError: connected=False
print(json.dumps({"connected":connected}),flush=True)
'''


def _assigned_ipv6_probe_source() -> str:
    return r'''import json,socket,struct,sys
cfg=json.load(sys.stdin); observed={}
for classification,target in cfg["targets"].items():
    try:
        client=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
        client.settimeout(2); client.connect((target["host"],target["port"],0,target["scope_id"]))
        client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
        observed[classification]=True
    except OSError: observed[classification]=False
print(json.dumps(observed,sort_keys=True),flush=True)
'''


def assess(role: str, observed: dict, expected_uid: int, *, controls=None) -> list[dict]:
    """Turn raw, non-secret observations into explicit acceptance checks."""
    identity = observed["identity"]; files = observed["file_interop"]
    network = observed["network"]; environment = observed["environment"]
    desktop = observed["desktop_entrypoints"]; model = observed["model_entrypoints"]
    functional = observed["functional"]
    controls=controls or {}
    private_control=controls.get("private_ipv4",{})
    private_available=private_control.get("available") is True
    clone_private_control=controls.get("clone_private_ipv4",{})
    clone_private_available=clone_private_control.get("available") is True
    public_ipv6_control=controls.get("public_ipv6",{})
    public_ipv6_available=public_ipv6_control.get("available") is True
    private_ipv6_control=controls.get("private_ipv6",{})
    private_ipv6_available=private_ipv6_control.get("available") is True
    assigned_ipv6=controls.get("clone_assigned_ipv6",{})
    assigned_targets=assigned_ipv6.get("targets",{})
    assigned_public=assigned_targets.get("public",{})
    assigned_private=assigned_targets.get("private",{})
    assigned_public_available=assigned_public.get("available") is True
    assigned_private_available=assigned_private.get("available") is True
    checks = [
        {"name":"run_uid_unprivileged", "passed":identity["uid"] == expected_uid and not identity["root"]},
        {"name":"windows_file_and_interop_entrypoints_absent", "passed":not any((files["mnt_c_mount"], files["host_sentinel_visible"], files["host_sentinel_readable"], files["cmd_exe_visible"], files["manual_drvfs_mount_succeeded"]))},
        {"name":"allowed_loopback_live_listener_reachable", "passed":network["allowed_loopback_live_listener"]},
        {"name":"wrong_loopback_live_listener_denied", "passed":not network["wrong_loopback_live_listener"]},
        {"name":"public_dns_policy", "passed":network["public_dns_resolved"] if role == "development" else not network["public_dns_resolved"]},
        {"name":"public_endpoint_policy", "passed":network["public_endpoint_connected"] if role == "development" else not network["public_endpoint_connected"]},
        {"name":"model_environment_absent", "passed":not environment["model_names_present"]},
        {"name":"desktop_environment_and_entrypoints_absent", "passed":not environment["desktop_names_present"] and not any(desktop.values())},
        {"name":"own_process_control", "passed":all(observed["own_process"].values())},
        {"name":"bash_child_works", "passed":functional["bash"]},
        {"name":"python_cv2_resize_works", "passed":functional["python_cv2_resize"]},
        {"name":"mcp_stdio_initialize_and_list_works", "passed":functional["mcp_stdio_initialize_and_list"]},
    ]
    if role == "development":
        checks.append({"name":"claude_version_works_without_model_call", "passed":functional["claude_version_exit_zero"] is True})
    if role == "final_test":
        checks.append({"name":"configured_model_cli_not_executable", "passed":not model["configured_agent_executable"]})
    checks.extend([
        {"name":"allowed_ipv6_loopback_live_listener_reachable",
         "passed":network["allowed_ipv6_loopback_live_listener"]},
        {"name":"wrong_ipv6_loopback_live_listener_denied",
         "passed":not network["wrong_ipv6_loopback_live_listener"]},
        {"name":"controlled_private_ipv4_denied","applicable":private_available,
         "claimed":private_available,
         "passed":not network["controlled_private_ipv4_connected"] if private_available else None,
         "reason":None if private_available else private_control.get("reason","positive control unavailable")},
        {"name":"clone_local_private_ipv4_denied","applicable":clone_private_available,
         "claimed":clone_private_available,
         "passed":not network["clone_local_private_ipv4_connected"] if clone_private_available else None,
         "reason":None if clone_private_available else clone_private_control.get("reason","assigned private IPv4 unavailable")},
        {"name":"controlled_public_ipv6_policy","applicable":public_ipv6_available,
         "claimed":public_ipv6_available,
         "passed":(((network["controlled_public_ipv6_connected"] is True and
                      isinstance(network.get("controlled_public_ipv6_tls"),dict) and
                      network["controlled_public_ipv6_tls"].get("tls_validated") is True)
                     if role == "development" else network["controlled_public_ipv6_connected"] is False)
                    if public_ipv6_available else None),
         "reason":None if public_ipv6_available else public_ipv6_control.get("reason","positive control unavailable")},
        {"name":"controlled_private_ipv6_denied","applicable":private_ipv6_available,
         "claimed":private_ipv6_available,
         "passed":not network["controlled_private_ipv6_connected"] if private_ipv6_available else None,
         "reason":None if private_ipv6_available else private_ipv6_control.get("reason","positive control unavailable")},
        {"name":"clone_local_assigned_public_ipv6_policy","applicable":assigned_public_available,
         "claimed":assigned_public_available,
         "passed":((network["clone_local_assigned_ipv6_connected"]["public"] if role == "development"
                    else not network["clone_local_assigned_ipv6_connected"]["public"])
                   if assigned_public_available else None),
         "reason":None if assigned_public_available else assigned_public.get("reason","assigned global IPv6 unavailable")},
        {"name":"clone_local_assigned_private_ipv6_denied","applicable":assigned_private_available,
         "claimed":assigned_private_available,
         "passed":not network["clone_local_assigned_ipv6_connected"]["private"] if assigned_private_available else None,
         "reason":None if assigned_private_available else assigned_private.get("reason","assigned private IPv6 unavailable")},
    ])
    return checks


def _checks_pass(checks: list[dict]) -> bool:
    return all(row["passed"] is True for row in checks if row.get("applicable",True))


def _collect(supervisor, handle: str, timeout: float = 20) -> dict:
    deadline=time.monotonic()+timeout; stdout=[]; stderr=[]
    while time.monotonic() < deadline:
        state=supervisor.poll(handle)
        for event in state["events"]:
            (stdout if event["stream"] == "stdout" else stderr).append(event["data"])
        if not state["running"] and state["output_complete"]:
            if state["returncode"] != 0:
                raise RuntimeError(f"probe exited {state['returncode']}: {''.join(stderr)[:500]}")
            return json.loads("".join(stdout))
        time.sleep(.05)
    supervisor.terminate(handle)
    raise TimeoutError("WSL runtime canary timed out")


def _launch_and_collect(supervisor, argv, *, cwd, env, stdin_text, timeout=20) -> dict:
    """Launch through the supervisor API and poll its returned handle."""
    child=supervisor.launch(argv,cwd=cwd,env=env,stdin_text=stdin_text)
    if not isinstance(child,dict) or not isinstance(child.get("handle"),str):
        raise RuntimeError("WSL supervisor launch returned no process handle")
    return _collect(supervisor,child["handle"],timeout)


def _verified_public_endpoint(host="pypi.org",port=443) -> dict:
    addresses=[]
    for item in socket.getaddrinfo(host,port,socket.AF_INET,socket.SOCK_STREAM):
        address=item[4][0]
        if address not in addresses: addresses.append(address)
    last_error=None
    for address in addresses[:4]:
        try:
            with socket.create_connection((address,port),3) as raw:
                with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as tls:
                    return {"host":host,"ipv4":address,"port":port,"host_tcp_tls":True,
                            "tls_version":tls.version()}
        except OSError as error: last_error=error
    raise RuntimeError(f"host public endpoint control failed: {type(last_error).__name__}")


def _public_ipv6_candidates(host="pypi.org",port=443,limit=4) -> dict:
    addresses=[]
    try: records=socket.getaddrinfo(host,port,socket.AF_INET6,socket.SOCK_STREAM)
    except OSError as error:
        return {"available":False,"host":host,"port":port,"addresses":[],"reason":type(error).__name__}
    for record in records:
        raw=record[4][0].split("%",1)[0]
        try: address=ipaddress.ip_address(raw)
        except ValueError: continue
        if address.version==6 and address.is_global and address.ipv4_mapped is None:
            canonical=str(address)
            if canonical not in addresses: addresses.append(canonical)
        if len(addresses)>=limit: break
    return {"available":bool(addresses),"host":host,"port":port,"addresses":addresses,
            "reason":None if addresses else "no_global_nonmapped_AAAA"}


def _clone_public_ipv6_tls_control(native, distro: str, candidates: dict) -> dict:
    if candidates.get("available") is not True:
        return {"available":False,"reason":"host_AAAA_candidates_unavailable"}
    code=r'''import json,signal,socket,ssl,sys,time
def deadline_expired(*_): raise TimeoutError("aggregate deadline")
signal.signal(signal.SIGALRM,deadline_expired); signal.setitimer(signal.ITIMER_REAL,7.75)
cfg=json.load(sys.stdin); deadline=time.monotonic()+7.5; failures=[]
for address in cfg["addresses"]:
    remaining=deadline-time.monotonic()
    if remaining<=0: break
    raw=None
    try:
        raw=socket.socket(socket.AF_INET6,socket.SOCK_STREAM); raw.settimeout(min(1,remaining))
        raw.connect((address,cfg["port"],0,0))
        remaining=deadline-time.monotonic()
        if remaining<=0: raise TimeoutError("aggregate deadline")
        raw.settimeout(min(1,remaining))
        with ssl.create_default_context().wrap_socket(raw,server_hostname=cfg["host"]) as tls:
            certificate=bool(tls.getpeercert())
            print(json.dumps({"available":certificate,"host":address,"port":cfg["port"],
              "server_hostname":cfg["host"],"tcp_tls":True,"certificate_validated":certificate,
              "tls_version":tls.version(),"transport":"direct_AF_INET6_TLS_no_HTTP_no_proxy"}),flush=True)
            raise SystemExit(0 if certificate else 3)
    except Exception as error:
        failures.append(type(error).__name__)
        if raw is not None:
            try: raw.close()
            except OSError: pass
print(json.dumps({"available":False,"reason":"no_clone_root_direct_ipv6_TLS_route",
                  "attempted":len(failures),"failure_types":failures}),flush=True)
'''
    payload={"host":candidates["host"],"port":candidates["port"],"addresses":candidates["addresses"]}
    try:
        completed=native.exec(distro,["/usr/bin/python3","-I","-B","-c",code],user="root",timeout=9,
                              input=json.dumps(payload).encode())
        value=json.loads(completed.stdout.decode())
    except Exception as error:
        return {"available":False,"reason":type(error).__name__}
    if value.get("available") is not True:
        return {"available":False,"reason":str(value.get("reason","invalid_control_result"))[:100],
                "attempted":value.get("attempted"),"failure_types":value.get("failure_types",[])[:4]}
    try: address=ipaddress.ip_address(value.get("host"))
    except ValueError: return {"available":False,"reason":"invalid_control_address"}
    if (address.version!=6 or not address.is_global or address.ipv4_mapped is not None or
            str(address) not in candidates["addresses"] or value.get("port")!=candidates["port"] or
            value.get("server_hostname")!=candidates["host"] or value.get("tcp_tls") is not True or
            value.get("certificate_validated") is not True or
            value.get("transport")!="direct_AF_INET6_TLS_no_HTTP_no_proxy" or
            not isinstance(value.get("tls_version"),str) or not value["tls_version"]):
        return {"available":False,"reason":"invalid_control_result"}
    return value


def _windows_private_ipv4(public_ipv4: str) -> dict:
    """Select the existing Windows source address for the verified public route."""
    try:
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
            probe.connect((public_ipv4,443)); address=probe.getsockname()[0]
        parsed=ipaddress.ip_address(address)
        if parsed.version != 4 or not parsed.is_private or parsed.is_loopback or parsed.is_link_local:
            return {"available":False,"reason":"selected_source_is_not_private_ipv4"}
        return {"available":True,"host":address,"route_target":public_ipv4,
                "method":"UDP route selection; no datagram sent"}
    except (OSError,ValueError) as error:
        return {"available":False,"reason":type(error).__name__}


def _private_ipv4_route_control(native, distro: str, host: str, port: int) -> dict:
    """Prove the clone can reach an existing Windows private address as root."""
    try:
        parsed=ipaddress.ip_address(host)
    except ValueError:
        return {"available":False,"reason":"invalid_windows_address"}
    if parsed.version != 4 or not parsed.is_private or parsed.is_loopback or parsed.is_link_local:
        return {"available":False,"reason":"windows_address_is_not_private_ipv4"}
    code=r'''import json,socket,struct,sys
host=sys.argv[1]; port=int(sys.argv[2])
try:
    client=socket.create_connection((host,port),2)
    client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
    print(json.dumps({"available":True,"host":host,"port":port,"root_connected":True}))
except Exception as error:
    print(json.dumps({"available":False,"reason":type(error).__name__}))
'''
    try:
        completed=native.exec(distro,["/usr/bin/python3","-I","-B","-c",code,host,str(port)],user="root",timeout=5)
        value=json.loads(completed.stdout.decode())
    except Exception as error:
        return {"available":False,"reason":type(error).__name__}
    if (value.get("available") is True and value.get("root_connected") is True and
            isinstance(value.get("host"),str) and type(value.get("port")) is int):
        return value
    return {"available":False,"reason":str(value.get("reason","invalid_control_result"))[:100]}


def _readline_with_timeout(stream, timeout: float) -> bytes:
    results=queue.Queue(maxsize=1)
    thread=threading.Thread(target=lambda:results.put(stream.readline()),daemon=True); thread.start()
    try: return results.get(timeout=timeout)
    except queue.Empty as error: raise TimeoutError("WSL root listener did not become ready") from error


@contextmanager
def _wsl_root_ipv6_listener(native, distro: str, allowed_port: int, excluded_ports=()):
    """Run a finite root-owned IPv6 loopback control in the exact fresh clone."""
    excluded=sorted(set(excluded_ports)|{allowed_port})
    code=r'''import errno,json,select,socket,struct,sys,time
allowed=int(sys.argv[1]); excluded={int(value) for value in sys.argv[2:]}
server=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
server.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
server.bind(("::1",allowed)); server.listen(4); server.settimeout(2)
wrong=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
wrong.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
while True:
    wrong.bind(("::1",0)); wrong_port=wrong.getsockname()[1]
    if wrong_port not in excluded: break
    wrong.close(); wrong=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
    wrong.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
wrong.listen(4); wrong.settimeout(2)
for listener,port in ((server,allowed),(wrong,wrong_port)):
    client=socket.create_connection(("::1",port),2)
    connection,_=listener.accept()
    client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
    connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
print(json.dumps({"ready":True,"allowed_port":allowed,"wrong_port":wrong_port,
                  "root_connected":True,"wrong_root_connected":True,
                  "v6only":True,"pid":__import__("os").getpid()}),flush=True)
deadline=time.monotonic()+30
while time.monotonic()<deadline:
    readable,_,_=select.select([server,wrong,sys.stdin],[],[],min(1,deadline-time.monotonic()))
    if sys.stdin in readable and not sys.stdin.buffer.read(1): break
    for listener in (server,wrong):
        if listener in readable:
            try: connection,_=listener.accept()
            except OSError as error:
                if error.errno in (errno.ECONNABORTED,errno.ECONNRESET,errno.ETIMEDOUT): continue
                raise
            connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
server.close(); wrong.close()
'''
    command=[native.executable,"--distribution",distro,"--user","root","--exec",
             "/usr/bin/python3","-I","-B","-u","-c",code,str(allowed_port),*map(str,excluded)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             close_fds=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        raw=_readline_with_timeout(process.stdout,5)
        try: ready=json.loads(raw.decode())
        except (UnicodeError,ValueError) as error: raise RuntimeError("invalid WSL root IPv6 listener handshake") from error
        if (ready.get("ready") is not True or ready.get("root_connected") is not True or
                ready.get("wrong_root_connected") is not True or
                ready.get("v6only") is not True or ready.get("allowed_port") != allowed_port or
                type(ready.get("wrong_port")) is not int or ready["wrong_port"] in excluded or
                type(ready.get("pid")) is not int or ready["pid"] <= 1):
            raise RuntimeError("unsafe WSL root IPv6 listener handshake")
        yield ready
    finally:
        if process.stdin:
            try: process.stdin.close()
            except OSError: pass
        try: process.wait(20)
        except subprocess.TimeoutExpired as error:
            raise RootListenerCleanupError(
                "owned WSL root IPv6 listener did not exit cooperatively; clone must be preserved") from error
        if process.returncode != 0:
            stderr=process.stderr.read(500).decode("utf-8","replace") if process.stderr else ""
            raise RuntimeError(f"WSL root IPv6 listener exited {process.returncode}: {stderr}")


@contextmanager
def _wsl_root_private_ipv4_listener(native, distro: str, allowed_port: int):
    """Bind an already assigned clone RFC1918 address without changing it."""
    code=r'''import errno,fcntl,ipaddress,json,select,socket,struct,sys,time
networks=tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"))
addresses=[]
probe=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
for _,name in socket.if_nameindex():
    if name=="lo": continue
    try:
        request=struct.pack("256s",name.encode()[:15])
        raw=fcntl.ioctl(probe.fileno(),0x8915,request)
        address=ipaddress.ip_address(socket.inet_ntoa(raw[20:24]))
        if any(address in network for network in networks) and not address.is_loopback and not address.is_link_local:
            addresses.append(str(address))
    except OSError: pass
probe.close()
if not addresses:
    print(json.dumps({"available":False,"reason":"no_assigned_nonloopback_rfc1918_ipv4"}),flush=True)
    raise SystemExit(0)
address=sorted(set(addresses))[0]; port=int(sys.argv[1])
server=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
server.bind((address,port)); server.listen(4)
client=socket.create_connection((address,port),2)
connection,_=server.accept()
client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
print(json.dumps({"available":True,"host":address,"port":port,"root_connected":True,
                  "pid":__import__("os").getpid(),"address_source":"assigned_interface_ioctl"}),flush=True)
deadline=time.monotonic()+30
while time.monotonic()<deadline:
    readable,_,_=select.select([server,sys.stdin],[],[],min(1,deadline-time.monotonic()))
    if sys.stdin in readable and not sys.stdin.buffer.read(1): break
    if server in readable:
        try: connection,_=server.accept()
        except OSError as error:
            if error.errno in (errno.ECONNABORTED,errno.ECONNRESET,errno.ETIMEDOUT): continue
            raise
        connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
server.close()
'''
    command=[native.executable,"--distribution",distro,"--user","root","--exec",
             "/usr/bin/python3","-I","-B","-u","-c",code,str(allowed_port)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             close_fds=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        raw=_readline_with_timeout(process.stdout,5)
        try: ready=json.loads(raw.decode())
        except (UnicodeError,ValueError) as error: raise RuntimeError("invalid WSL root private IPv4 listener handshake") from error
        if ready.get("available") is False:
            if ready != {"available":False,"reason":"no_assigned_nonloopback_rfc1918_ipv4"}:
                raise RuntimeError("unsafe unavailable private IPv4 control")
            yield ready
        else:
            try: address=ipaddress.ip_address(ready.get("host"))
            except ValueError as error: raise RuntimeError("invalid assigned private IPv4 control address") from error
            rfc1918=any(address in ipaddress.ip_network(value) for value in (
                "10.0.0.0/8","172.16.0.0/12","192.168.0.0/16"))
            if (ready.get("available") is not True or ready.get("root_connected") is not True or
                    ready.get("address_source") != "assigned_interface_ioctl" or not rfc1918 or
                    address.is_loopback or address.is_link_local or ready.get("port") != allowed_port or
                    type(ready.get("pid")) is not int or ready["pid"] <= 1):
                raise RuntimeError("unsafe WSL root private IPv4 listener handshake")
            yield ready
    finally:
        if process.stdin:
            try: process.stdin.close()
            except OSError: pass
        try: process.wait(20)
        except subprocess.TimeoutExpired as error:
            raise RootListenerCleanupError(
                "owned WSL root private IPv4 listener did not exit cooperatively; clone must be preserved") from error
        if process.returncode != 0:
            stderr=process.stderr.read(500).decode("utf-8","replace") if process.stderr else ""
            raise RuntimeError(f"WSL root private IPv4 listener exited {process.returncode}: {stderr}")


@contextmanager
def _wsl_root_assigned_ipv6_listeners(native, distro: str, allowed_port: int):
    """Bind existing clone global/private IPv6 addresses without changing them."""
    code=r'''import errno,ipaddress,json,select,socket,struct,sys,time
port=int(sys.argv[1]); rows=[]
with open("/proc/net/if_inet6",encoding="ascii") as source:
    for line in source:
        fields=line.split()
        if len(fields)!=6: continue
        raw,index_hex,prefix_hex,scope_hex,flags_hex,name=fields
        try:
            address=ipaddress.IPv6Address(int(raw,16)); ifindex=int(index_hex,16)
            prefix=int(prefix_hex,16); scope=int(scope_hex,16); flags=int(flags_hex,16)
        except ValueError: continue
        if name=="lo" or address.is_loopback or flags & (0x40|0x08): continue
        try:
            if socket.if_indextoname(ifindex)!=name: continue
        except OSError: continue
        rows.append((address,ifindex,prefix,scope,flags,name))
global_rows=[row for row in rows if row[0].is_global and row[3]==0]
ula=ipaddress.ip_network("fc00::/7")
ula_rows=[row for row in rows if row[0] in ula and row[3]==0]
link_rows=[row for row in rows if row[0].is_link_local and row[3]==0x20]
def choose(values): return sorted(values,key=lambda row:(bool(row[4]&0x20),str(row[0]),row[1]))[0] if values else None
selected={"public":choose(global_rows),"private":choose(ula_rows) or choose(link_rows)}
targets={}; listeners={}
for classification,row in selected.items():
    if row is None:
        targets[classification]={"available":False,"reason":"no_qualified_assigned_"+classification+"_ipv6"}; continue
    address,ifindex,prefix,scope,flags,name=row
    scope_id=ifindex if address.is_link_local else 0
    listener=socket.socket(socket.AF_INET6,socket.SOCK_STREAM)
    listener.setsockopt(socket.IPPROTO_IPV6,socket.IPV6_V6ONLY,1)
    listener.bind((str(address),port,0,scope_id)); listener.listen(4)
    client=socket.socket(socket.AF_INET6,socket.SOCK_STREAM); client.settimeout(2)
    client.connect((str(address),port,0,scope_id)); connection,_=listener.accept()
    client.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); client.close()
    connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
    address_class=("global" if classification=="public" else ("ula" if address in ula else "link_local"))
    targets[classification]={"available":True,"host":str(address),"port":port,"scope_id":scope_id,
      "ifindex":ifindex,"interface":name,"prefix_length":prefix,"kernel_scope":scope,"flags":flags,
      "address_class":address_class,"address_source":"proc_net_if_inet6","root_connected":True}
    listeners[classification]=listener
print(json.dumps({"targets":targets,"pid":__import__("os").getpid(),
                  "v6only":True,"address_source":"proc_net_if_inet6"},sort_keys=True),flush=True)
if listeners:
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        watched=list(listeners.values())+[sys.stdin]
        readable,_,_=select.select(watched,[],[],min(1,deadline-time.monotonic()))
        if sys.stdin in readable and not sys.stdin.buffer.read(1): break
        for listener in listeners.values():
            if listener in readable:
                try: connection,_=listener.accept()
                except OSError as error:
                    if error.errno in (errno.ECONNABORTED,errno.ECONNRESET,errno.ETIMEDOUT): continue
                    raise
                connection.setsockopt(socket.SOL_SOCKET,socket.SO_LINGER,struct.pack("ii",1,0)); connection.close()
for listener in listeners.values(): listener.close()
'''
    command=[native.executable,"--distribution",distro,"--user","root","--exec",
             "/usr/bin/python3","-I","-B","-u","-c",code,str(allowed_port)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             close_fds=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        raw=_readline_with_timeout(process.stdout,5)
        try: ready=json.loads(raw.decode())
        except (UnicodeError,ValueError) as error: raise RuntimeError("invalid assigned IPv6 listener handshake") from error
        if (set(ready) != {"targets","pid","v6only","address_source"} or
                ready.get("v6only") is not True or ready.get("address_source") != "proc_net_if_inet6" or
                type(ready.get("pid")) is not int or ready["pid"] <= 1 or
                not isinstance(ready.get("targets"),dict) or set(ready["targets"]) != {"public","private"}):
            raise RuntimeError("unsafe assigned IPv6 listener handshake")
        for classification,target in ready["targets"].items():
            if target.get("available") is False:
                expected=f"no_qualified_assigned_{classification}_ipv6"
                if target != {"available":False,"reason":expected}:
                    raise RuntimeError("unsafe unavailable assigned IPv6 control")
                continue
            try: address=ipaddress.ip_address(target.get("host"))
            except ValueError as error: raise RuntimeError("invalid assigned IPv6 control address") from error
            valid_class=(classification=="public" and address.version==6 and address.is_global and
                         target.get("address_class")=="global" and target.get("scope_id")==0)
            valid_private=(classification=="private" and address.version==6 and
                           ((address in ipaddress.ip_network("fc00::/7") and target.get("address_class")=="ula" and target.get("scope_id")==0) or
                            (address.is_link_local and target.get("address_class")=="link_local" and
                             target.get("scope_id")==target.get("ifindex"))))
            if (not (valid_class or valid_private) or target.get("root_connected") is not True or
                    target.get("address_source") != "proc_net_if_inet6" or target.get("port") != allowed_port or
                    type(target.get("ifindex")) is not int or target["ifindex"] <= 0 or
                    type(target.get("flags")) is not int or target["flags"] & (0x40|0x08)):
                raise RuntimeError("unsafe assigned IPv6 target metadata")
        yield ready
    finally:
        if process.stdin:
            try: process.stdin.close()
            except OSError: pass
        try: process.wait(20)
        except subprocess.TimeoutExpired as error:
            raise RootListenerCleanupError(
                "owned assigned IPv6 listeners did not exit cooperatively; clone must be preserved") from error
        if process.returncode != 0:
            stderr=process.stderr.read(500).decode("utf-8","replace") if process.stderr else ""
            raise RuntimeError(f"assigned IPv6 listener exited {process.returncode}: {stderr}")


def _run_ipv6_phase(supervisor, native, distro: str, python, staged, allowed_port: int, excluded_ports=()):
    """Complete clone-local IPv6 controls before any host listener phase."""
    with _wsl_root_ipv6_listener(native,distro,allowed_port,excluded_ports) as control:
        observed=_launch_and_collect(
            supervisor,[str(python),"-I","-B",str(staged/"ipv6_probe.py")],cwd=staged,
            env={"C4G_CANARY_ROLE":supervisor.role},stdin_text=json.dumps({
                "allowed_port":allowed_port,"wrong_port":control["wrong_port"]}))
    if set(observed) != {"allowed_connected","wrong_connected"} or any(
            type(value) is not bool for value in observed.values()):
        raise RuntimeError("invalid IPv6 probe observations")
    return control,observed


def _run_private_ipv4_phase(supervisor, native, distro: str, python, staged, allowed_port: int):
    """Probe an assigned clone-private address under the verified run UID."""
    with _wsl_root_private_ipv4_listener(native,distro,allowed_port) as control:
        if control.get("available") is not True:
            return control,None
        observed=_launch_and_collect(
            supervisor,[str(python),"-I","-B",str(staged/"private_ipv4_probe.py")],cwd=staged,
            env={"C4G_CANARY_ROLE":supervisor.role},stdin_text=json.dumps({
                "host":control["host"],"port":control["port"]}))
    if set(observed) != {"connected"} or type(observed["connected"]) is not bool:
        raise RuntimeError("invalid private IPv4 probe observation")
    return control,observed


def _run_assigned_ipv6_phase(supervisor, native, distro: str, python, staged, allowed_port: int):
    """Probe assigned IPv6 address classes under the verified run UID."""
    with _wsl_root_assigned_ipv6_listeners(native,distro,allowed_port) as control:
        available={name:target for name,target in control["targets"].items()
                   if target.get("available") is True}
        if not available: return control,{}
        payload={name:{"host":target["host"],"port":target["port"],"scope_id":target["scope_id"]}
                 for name,target in available.items()}
        observed=_launch_and_collect(
            supervisor,[str(python),"-I","-B",str(staged/"assigned_ipv6_probe.py")],cwd=staged,
            env={"C4G_CANARY_ROLE":supervisor.role},stdin_text=json.dumps({"targets":payload}))
    if set(observed) != set(available) or any(type(value) is not bool for value in observed.values()):
        raise RuntimeError("invalid assigned IPv6 probe observations")
    return control,observed


def preview(profile: WslProfile, out: Path) -> dict:
    return {"schema":SCHEMA,"execute":False,"profile_id":profile.id,"profile":str(profile.baseline.parent.parent / f"profile-{profile.id}.json"),
            "out":str(out.resolve()),"roles":["development","final_test"],"model_calls":0,"game_started":False,
            "note":"add --execute to acquire the profile lock and create fresh per-run clones"}


def execute(profile: WslProfile, out: Path, *, runtime_factory=WslRuntime) -> dict:
    out=out.resolve()
    if out.exists(): raise ValueError("--out must be a new evidence directory")
    runtime=runtime_factory(profile,evidence_dir=out)
    result={"schema":SCHEMA,"status":"running","profile_id":profile.id,"baseline_sha256":profile.baseline_sha256,
            "model_calls":0,"game_started":False,"roles":{},"limitations":[
                "Absent desktop entrypoints do not prove complete desktop isolation.",
                "CLI, environment, and network checks do not prove that submitted artifacts contain no local inference implementation or model weights; artifact review remains required.",
                "The legacy 192.168.255.254:9 result has no positive control and remains connectivity observation only. Controlled private-address denial is claimed only when the same clone first reaches the live listener as root.",
                "The process canary controls only a child created by the probe; it does not signal or inspect unrelated processes."]}
    evidence=out/"result.json"
    def save(): evidence.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    try:
        with runtime:
            # Acquiring the profile lock precedes even evidence-directory creation,
            # so a busy profile is refused without leaving a partial probe behind.
            out.mkdir(parents=True)
            sentinel=out/"harmless-host-sentinel.bin"
            sentinel.write_bytes(b"C4G harmless WSL file visibility sentinel\n")
            save()
            result["preflight"]=runtime.preflight(protected=(out,))
            result["public_endpoint_control"]=_verified_public_endpoint()
            result["windows_private_ipv4_control"]=_windows_private_ipv4(
                result["public_endpoint_control"]["ipv4"])
            result["windows_ipv6_loopback_control"]={"available":False,
                "reason":"Windows ::1 reachability is not used as evidence for clone-local IPv6 loopback"}
            result["public_ipv6_candidates"]=_public_ipv6_candidates()
            result["private_ipv6_control"]={"available":False,
                "reason":"no existing assigned host ULA or link-local address is selected by this probe"}
            save()
            for role in ("development","final_test"):
                supervisor=runtime.start(role); role_evidence=runtime.role_evidence(role)
                diagnostic_log=str(supervisor.diagnostic_path)
                result["roles"][role]={"runtime":role_evidence,"diagnostic_log":diagnostic_log,"status":"running"}
                save()
                allowed_port=profile.ports[f"{role}_game"]
                # Port zero asks the OS for a live control listener that cannot
                # collide with any fixed profile port. Keep both listeners open
                # for the entire guest connection attempt.
                excluded=set(profile.ports.values())
                client=(REPO/"coding4game_gui"/"runtime_client"/"client.py").read_bytes()
                staged=supervisor.stage({"probe.py":_probe_source().encode(),
                                         "ipv6_probe.py":_ipv6_probe_source().encode(),
                                         "private_ipv4_probe.py":_private_ipv4_probe_source().encode(),
                                         "assigned_ipv6_probe.py":_assigned_ipv6_probe_source().encode(),
                                         "client.py":client},name="canaries")
                try:
                    # WSL mirrors some host port reservations. Complete and stop
                    # the clone-local IPv6 controls before binding Windows IPv4
                    # listeners on the same approved numeric port.
                    ipv6_control,ipv6_observed=_run_ipv6_phase(
                        supervisor,runtime.native,role_evidence["distro"],profile.python,staged,
                        allowed_port,excluded)
                    result["roles"][role]["network_phases"]={
                        "ipv6":{"root_control":ipv6_control,"run_uid_observed":ipv6_observed,
                                "verified_run_uid":role_evidence["isolation"]["uid"],
                                "root_helper_stopped_before_windows_ipv4":True}}
                    save()
                    clone_private_control,clone_private_observed=_run_private_ipv4_phase(
                        supervisor,runtime.native,role_evidence["distro"],profile.python,staged,allowed_port)
                    result["roles"][role]["network_phases"]["clone_private_ipv4"]={
                        "root_control":clone_private_control,"run_uid_observed":clone_private_observed,
                        "verified_run_uid":role_evidence["isolation"]["uid"],
                        "claimed":clone_private_control.get("available") is True}
                    save()
                    assigned_ipv6_control,assigned_ipv6_observed=_run_assigned_ipv6_phase(
                        supervisor,runtime.native,role_evidence["distro"],profile.python,staged,allowed_port)
                    result["roles"][role]["network_phases"]["clone_assigned_ipv6"]={
                        "root_control":assigned_ipv6_control,"run_uid_observed":assigned_ipv6_observed,
                        "verified_run_uid":role_evidence["isolation"]["uid"],
                        "scope":"clone-local assigned addresses; not external Internet IPv6 reachability"}
                    save()
                    external_public_ipv6_control=_clone_public_ipv6_tls_control(
                        runtime.native,role_evidence["distro"],result["public_ipv6_candidates"])
                    result["roles"][role]["network_phases"]["external_public_ipv6"]={
                        "root_control":external_public_ipv6_control,
                        "scope":"fresh-clone root direct numeric AF_INET6 TLS; no HTTP, download, or proxy"}
                    save()
                    with ExitStack() as stack:
                        ipv4_bind_readiness={}
                        stack.enter_context(_listener(
                            allowed_port,bind_timeout=10,readiness=ipv4_bind_readiness))
                        wrong_port=stack.enter_context(_listener(0,excluded_ports=excluded))
                        wrong_ipv6_port=ipv6_control["wrong_port"]
                        private_port=stack.enter_context(_listener(
                            0,host="0.0.0.0",excluded_ports=excluded|{wrong_port,wrong_ipv6_port}))
                        windows_private=result["windows_private_ipv4_control"]
                        private_control=(_private_ipv4_route_control(
                            runtime.native,role_evidence["distro"],windows_private["host"],private_port)
                            if windows_private.get("available") is True else
                            {"available":False,"reason":"Windows private route source unavailable"})
                        controls={"loopback_ipv6":ipv6_control,"private_ipv4":private_control,
                                  "clone_private_ipv4":clone_private_control,
                                  "clone_assigned_ipv6":assigned_ipv6_control,
                                  "public_ipv6":external_public_ipv6_control,
                                  "private_ipv6":result["private_ipv6_control"]}
                        payload={"host_sentinel":_windows_to_wsl(sentinel),"workspace":str(supervisor.workspace),"role":role,
                                 "allowed_port":allowed_port,"wrong_port":wrong_port,
                                 "ipv6_observed":ipv6_observed,
                                 "clone_private_ipv4_observed":(
                                     clone_private_observed["connected"] if clone_private_observed is not None else None),
                                 "clone_assigned_ipv6_observed":assigned_ipv6_observed,
                                 "private_ipv4_target":({"host":private_control["host"],"port":private_control["port"]}
                                                        if private_control.get("available") is True else None),
                                 "public_ipv6_target":({"host":external_public_ipv6_control["host"],
                                                        "port":external_public_ipv6_control["port"],
                                                        "server_hostname":external_public_ipv6_control["server_hostname"]}
                                                       if external_public_ipv6_control.get("available") is True else None),
                                 "private_ipv6_target":None,
                                 "model_env_names":sorted(MODEL_ENV_NAMES),
                                 "agent_path":str(profile.agent("claude")),
                                 "public_host":result["public_endpoint_control"]["host"],
                                 "public_ipv4":result["public_endpoint_control"]["ipv4"]}
                        payload["mcp_client"]=str(staged/"client.py")
                        observed=_launch_and_collect(
                            supervisor,[str(profile.python),"-I","-B",str(staged/"probe.py")],cwd=staged,
                            env={"C4G_CANARY_ROLE":role},stdin_text=json.dumps(payload))
                except RootListenerCleanupError as error:
                    # Do not unregister a clone whose owned root helper could not
                    # be proven stopped. The finite helper may still self-exit.
                    runtime._lifecycle(role_evidence["distro"],status="failed_preserved",
                                       error_type=type(error).__name__)
                    runtime._clones.pop(role,None)
                    raise
                checks=assess(role,observed,role_evidence["isolation"]["uid"],controls=controls)
                network_phases=result["roles"][role]["network_phases"]
                network_phases["windows_ipv4"]={"started_after_ipv6_root_helper_stopped":True,
                                                 "approved_bind":ipv4_bind_readiness,
                                                 "private_control":private_control}
                result["roles"][role]={"runtime":role_evidence,"diagnostic_log":diagnostic_log,
                                       "network_phases":network_phases,"network_controls":controls,
                                       "observed":observed,"checks":checks,
                                       "passed":_checks_pass(checks)}
                save()
        result["runtime_exports"]=runtime.archives
        result["status"]="passed_with_documented_limitations" if all(v["passed"] for v in result["roles"].values()) else "failed"
    except BaseException as error:
        result.update(status="failed",error_type=type(error).__name__,error=str(error)[:1000])
        if getattr(runtime,"archives",None):
            result["runtime_exports"]=list(runtime.archives)
        if out.is_dir(): save()
        raise
    save()
    return result


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",type=Path,required=True); parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--execute",action="store_true"); args=parser.parse_args()
    profile=WslProfile.load(args.profile)
    if not args.execute:
        print(json.dumps(preview(profile,args.out),indent=2)); return 0
    result=execute(profile,args.out)
    print(json.dumps({"status":result["status"],"evidence":str((args.out.resolve()/"result.json"))},indent=2))
    return 0 if result["status"].startswith("passed") else 1


if __name__ == "__main__": raise SystemExit(main())
