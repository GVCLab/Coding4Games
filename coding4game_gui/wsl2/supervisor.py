from __future__ import annotations

import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from coding4game_gui.runtime_client.bundle import decode_files, encode_files
from coding4game_gui.runtime_client import wire
from .runtime_error import WslTransportError

_SECRET_WORDS = ("API_KEY", "PASSWORD", "CREDENTIAL", "SECRET", "OAUTH")
CLAUDE_TRANSPORT_ENV = {
    "API_TIMEOUT_MS": "1860000",
    "BUN_CONFIG_HTTP_IDLE_TIMEOUT": "1800",
    "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS": "1800000",
    "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": "1800000",
    "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "1800000",
}
_MODEL_ENV = {"OPENAI_BASE_URL","CODEX_HOME","C4G_GATEWAY_TOKEN","ANTHROPIC_BASE_URL","ANTHROPIC_AUTH_TOKEN",
              "ANTHROPIC_API_KEY","CLAUDE_CONFIG_DIR","GOOGLE_GEMINI_BASE_URL","GEMINI_API_KEY"}
_MODEL_ENV.update(CLAUDE_TRANSPORT_ENV)
_WINDOWS_TRANSIENT_BIND_ERRORS = {10013, 10048}


class WslControlBindError(WslTransportError):
    pass


def _bind_control_listener(port, *, bind_timeout=10, socket_factory=socket.socket,
                           monotonic=time.monotonic, sleep=time.sleep):
    """Acquire one fixed Windows loopback endpoint without weakening exclusivity."""
    host="127.0.0.1"; started=monotonic(); deadline=started+bind_timeout; attempts=0
    while True:
        attempts += 1
        listener=socket_factory()
        try:
            listener.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            listener.bind((host,port))
        except OSError as error:
            listener.close()
            winerror=getattr(error,"winerror",None)
            if winerror not in _WINDOWS_TRANSIENT_BIND_ERRORS:
                raise
            if monotonic() >= deadline:
                waited=round(monotonic()-started,3)
                raise WslControlBindError(
                    f"WSL control listener bind failed for {host}:{port} "
                    f"(attempts={attempts}; bind_wait_seconds={waited}; winerror={winerror})") from error
            sleep(.1)
            continue
        waited=round(monotonic()-started,3)
        return listener,{"host":host,"port":port,"attempts":attempts,
                         "bind_wait_seconds":waited}

_ISOLATE = b'''#!/bin/sh
set -eu
cg=$1; uid=$2; workspace=$3; python=$4; worker=$5; port=$6; host_mnt=$7; resolv_target=$8
actual_resolv_target=$(readlink -f /etc/resolv.conf)
test "$actual_resolv_target" = "$resolv_target"
test -s /etc/resolv.conf
case "$resolv_target" in
  /mnt/*|/run/*|/tmp/*) resolv_private=true; exec 9</etc/resolv.conf ;;
  /etc/*|/var/*) resolv_private=false ;;
  *) exit 72 ;;
esac
mount --make-rprivate /
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /mnt
mount -t tmpfs -o mode=0755,nosuid,nodev tmpfs /run
mount -t tmpfs -o mode=0755,nosuid tmpfs /dev
mknod -m 0666 /dev/null c 1 3
mknod -m 0666 /dev/zero c 1 5
mknod -m 0666 /dev/full c 1 7
mknod -m 0666 /dev/random c 1 8
mknod -m 0666 /dev/urandom c 1 9
mknod -m 0666 /dev/tty c 5 0
mkdir -m 0755 /dev/pts
mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts /dev/pts
ln -s pts/ptmx /dev/ptmx
ln -s /proc/self/fd /dev/fd
ln -s /proc/self/fd/0 /dev/stdin
ln -s /proc/self/fd/1 /dev/stdout
ln -s /proc/self/fd/2 /dev/stderr
mkdir -m 1777 /dev/shm
mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /dev/shm
if "$resolv_private"; then
  mkdir -p "$(dirname "$resolv_target")"
  cat <&9 > "$resolv_target"
  exec 9<&-
  chown root:root "$resolv_target"
  chmod 0444 "$resolv_target"
fi
test -s /etc/resolv.conf
test "$(readlink /proc/self/ns/mnt)" != "$host_mnt"
test ! -e /mnt/wslg
test ! -e /tmp/.X11-unix
test ! -e /dev/dxg
install -d -o "$uid" -g "$uid" -m 0700 "$workspace"
echo $$ > "$cg/cgroup.procs"
exec setpriv --reuid="$uid" --regid="$uid" --init-groups "$python" -I -B "$worker" "$port" "$workspace"
'''


def _resolver_target_scope(value):
    if not isinstance(value,str) or not value.startswith("/") or ".." in PurePosixPath(value).parts:
        raise WslTransportError("unsafe WSL resolver target")
    path=PurePosixPath(value)
    if path.parts[1:2] in (("mnt",),("run",),("tmp",)):
        return "private_recreated"
    if path.parts[1:2] in (("etc",),("var",)):
        return "unmasked_retained"
    raise WslTransportError("unsupported WSL resolver target")


def _mount_isolation_from_hello(hello, host_mount_namespace):
    mount_namespace=hello.get("mount_namespace")
    desktop=hello.get("desktop_entrypoints")
    devices=hello.get("essential_devices")
    resolver=hello.get("resolver")
    if (not isinstance(mount_namespace,str) or not mount_namespace or mount_namespace==host_mount_namespace or
            desktop != {"/dev/dxg":False,"/mnt/wslg":False,"/tmp/.X11-unix":False} or
            devices != {"null":True,"pts":True,"random":True,"shm":True,"urandom":True,"zero":True} or
            not isinstance(resolver,dict) or resolver.get("available") is not True or
            resolver.get("scope") != _resolver_target_scope(resolver.get("target")) or
            (resolver.get("scope")=="private_recreated" and resolver.get("mode")!="0o444")):
        raise WslTransportError("WSL private mount isolation verification failed")
    return {"private_mount_namespace":True,"mount_namespace":mount_namespace,
            "desktop_entrypoints":desktop,"essential_devices":devices,"resolver":resolver}


def _handshake_error(hello, token, run_uid):
    observed_token=hello.get("token")
    token_present=isinstance(observed_token,str) and bool(observed_token)
    token_matches=token_present and secrets.compare_digest(observed_token,token)
    observed_uid=hello.get("uid")
    uid_matches=type(observed_uid) is int and observed_uid == run_uid
    if token_matches and uid_matches: return None
    safe_uid=observed_uid if type(observed_uid) is int else None
    return ("WSL supervisor handshake failed "
            f"(token_present={token_present}; token_matches={token_matches}; "
            f"expected_uid={run_uid}; observed_uid={safe_uid}; uid_matches={uid_matches})")


class WslSupervisor:
    def __init__(self, runtime, role, distro):
        self.runtime, self.role, self.distro, self.closed = runtime, role, distro, False
        self._dependency_preparing = False
        self._dependency_failed = False
        self._final_execution_started = False
        self._dependency_manager = None
        self._call_lock = threading.Lock(); self._child_cgroups={}; token = secrets.token_hex(32)
        self.workspace = getattr(runtime, "_restored_workspaces", {}).get(
            role, PurePosixPath(f"/home/{runtime.profile.user}/run/{secrets.token_hex(12)}"))
        port = runtime.profile.control_port(role)
        diagnostic_root=(runtime.evidence_dir or runtime.profile.root/"diagnostics")/"supervisor"
        diagnostic_root.mkdir(parents=True,exist_ok=True)
        self.diagnostic_path=diagnostic_root/f"{role}-{distro}.log"
        worker = Path(__file__).with_name("worker.py").read_bytes()
        runtime.native.exec(distro, ["/bin/sh","-c","install -o root -g root -m 0555 /dev/stdin /opt/c4g-wsl-worker.py"], input=worker)
        runtime.native.exec(distro, ["/bin/sh","-c","install -o root -g root -m 0500 /dev/stdin /opt/c4g-wsl-isolate.sh"], input=_ISOLATE)
        try:
            listener,self.control_listener_readiness=_bind_control_listener(port)
            with listener:
                listener.listen(1); listener.settimeout(30)
                cgroup=runtime._cgroups[role]["cgroup"]
                run_uid=runtime._cgroups[role]["uid"]
                host_mount_namespace=runtime.native.mount_namespace(distro)
                resolver_target=runtime.native.resolver_target(distro)
                _resolver_target_scope(resolver_target)
                # setsid may fork when WSL starts it as a process-group leader.
                # --wait keeps the WSL invocation and its stdio alive until the
                # private-namespace worker exits.
                command=[runtime.native.executable,"--distribution",distro,"--user","root","--exec","setsid","--wait","/usr/bin/unshare",
                         "--mount","--propagation","private","--","/opt/c4g-wsl-isolate.sh",cgroup,str(run_uid),
                         str(self.workspace),str(runtime.profile.python),"/opt/c4g-wsl-worker.py",str(port),
                         host_mount_namespace,resolver_target]
                diagnostic=self.diagnostic_path.open("ab",buffering=0)
                self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=diagnostic,stderr=diagnostic,
                                              close_fds=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                diagnostic.close()
                self.process.stdin.write((token+"\n").encode()); self.process.stdin.close()
                self.sock,_=listener.accept(); hello=wire.receive(self.sock,10)
                handshake_error=_handshake_error(hello,token,run_uid)
                if handshake_error: raise WslTransportError(handshake_error)
                worker_pid=hello.get("pid")
                base_cgroup=runtime._cgroups[role]["cgroup"]
                worker_identity=runtime.native.verify_child(distro,worker_pid,run_uid,base_cgroup)
                host_verified=runtime.native.verify_private_mount(
                    distro,worker_pid,host_mount_namespace,resolver_target,_resolver_target_scope(resolver_target))
                reported=_mount_isolation_from_hello(hello,host_mount_namespace)
                if reported != host_verified or worker_identity["mount_namespace"] != host_verified["mount_namespace"]:
                    raise WslTransportError("WSL mount isolation evidence disagrees")
                self.mount_isolation=host_verified
                self.capabilities=hello.get("capabilities",[])
        except BaseException:
            self.close(); raise

    def call(self,op,args=None,timeout=60):
        if self.closed: raise WslTransportError("WSL supervisor is closed")
        try:
            with self._call_lock:
                wire.send(self.sock,{"op":op,"args":args or {}},timeout); reply=wire.receive(self.sock,timeout)
        except (OSError,EOFError,ValueError,TimeoutError) as exc:
            self.close(); raise WslTransportError(f"WSL transport failed: {exc}") from exc
        if reply.get("ok") is not True: raise RuntimeError(str(reply.get("error","supervisor failed"))[:4000])
        return reply["value"]

    def native_path(self,directory,*parts):
        base=PurePosixPath(directory)
        for part in parts:
            candidate=PurePosixPath(part)
            if candidate.is_absolute() or ".." in candidate.parts: raise ValueError("path escapes WSL workspace")
            base /= candidate
        return str(base)

    def stage(self,files,name="artifact"):
        return PurePosixPath(self.call("stage",{"files":encode_files(files),"name":name})["path"])
    def stage_connection(self,files,name="artifact"):
        """Refresh only run-scoped configuration after verified workspace restore."""
        if self.role not in getattr(self.runtime, "_restored_workspaces", {}):
            return self.stage(files,name=name)
        allowed={"mcp":{"client.py","c4g_client.py"},
                 ".claude":{"mcp.json","settings.json"},".codex":{"config.toml"}}
        if self.role != "development" or name not in allowed or not set(files)<=allowed[name]:
            raise ValueError("only restored development connection files can be refreshed")
        return PurePosixPath(self.call("refresh_connection",{
            "files":encode_files(files),"name":name})["path"])
    def read_tree(self,path): return decode_files(self.call("read_tree",{"path":str(path)})["files"])

    def launch(self,argv,cwd=None,env=None,*,stdin_text=None):
        if self._dependency_preparing or self._dependency_failed:
            raise WslTransportError("dependency preparation has not sealed the execution environment")
        if self.role == "final_test": self._final_execution_started = True
        return self._launch(argv,cwd,env,stdin_text=stdin_text)

    def _launch_dependency(self,argv,cwd=None,env=None):
        return self._launch(argv,cwd,env)

    def freeze_dependencies(self,files,dependencies,deadline):
        return self._dependencies().freeze(files,dependencies,deadline)

    def prepare_program(self,record,directory,deadline):
        return self._dependencies().prepare(record,directory,deadline)

    def _dependencies(self):
        if self._dependency_manager is None:
            from .dependencies import DependencyManager
            self._dependency_manager = DependencyManager(self)
        return self._dependency_manager

    def _launch(self,argv,cwd=None,env=None,*,stdin_text=None,output_mode="bounded"):
        if not isinstance(argv,list) or not argv or any(not isinstance(x,str) for x in argv): raise ValueError("argv must be a nonempty string list")
        if output_mode not in ("bounded","stream"): raise ValueError("invalid output mode")
        if output_mode=="stream" and self.role!="development": raise ValueError("streamed CLI output is development-only")
        supplied=dict(env or {})
        if self.role=="final_test" and any(any(w in k.upper() for w in _SECRET_WORDS) or k in _MODEL_ENV for k in supplied):
            raise ValueError("credentials/model configuration forbidden in final_test")
        value=self.call("launch",{"argv":argv,"cwd":str(cwd or self.workspace),"env":supplied,"stdin_text":stdin_text,"output_mode":output_mode})
        try:
            isolation=self.runtime._cgroups[self.role]
            self.runtime.native.verify_child(self.distro,value.get("pid"),isolation["uid"],
                                             isolation["cgroup"],self.mount_isolation["mount_namespace"])
            child_cgroup=self.runtime.native.assign_child_cgroup(
                self.distro,isolation,value["handle"],value.get("pid"))
            self._child_cgroups[value["handle"]]=child_cgroup
            self.runtime.native.verify_child(self.distro,value.get("pid"),isolation["uid"],
                                             child_cgroup,self.mount_isolation["mount_namespace"])
            self.call("resume",{"handle":value["handle"]},10)
        except BaseException:
            try:self.call("terminate",{"handle":value.get("handle")},10)
            finally:self.close()
            raise
        value["suspended"]=False
        return value

    def launch_agent(self,agent,argv,*,gateway,cwd=None,connection_env=None,stdin_text=None):
        if self.role!="development": raise ValueError("model CLI is development-only")
        if self._dependency_preparing or self._dependency_failed:
            raise WslTransportError("dependency preparation has not sealed the execution environment")
        if agent not in {"claude", "codex"} or not argv or PurePosixPath(argv[0]) != self.runtime.profile.agent(agent):
            raise ValueError("WSL2 requires a configured Linux Claude or Codex CLI")
        parsed=urlsplit(str(gateway.get("url",""))) if isinstance(gateway,dict) else None
        if (not isinstance(gateway,dict) or not {"url","token","port"} <= set(gateway) or
                gateway["port"] != self.runtime.profile.ports["gateway"] or parsed.scheme!="http" or
                parsed.hostname not in {"127.0.0.1","localhost"} or parsed.port != gateway["port"] or
                not isinstance(gateway["token"],str) or len(gateway["token"])<32):
            raise ValueError("invalid scoped gateway lease")
        extra=dict(connection_env or {}); allowed={"C4G_BRIDGE_URL","C4G_BRIDGE_TOKEN","C4G_EPISODE_ID","C4G_BRIDGE_PHASE"}
        if any(k not in allowed or not isinstance(v,str) for k,v in extra.items()): raise ValueError("unsupported connection environment")
        extra.update({"HOME":str(self.workspace),"PYTHONPATH":str(self.workspace/"mcp"),
                      "C4G_PYTHON":str(self.runtime.profile.python)})
        if agent == "claude":
            # Let the host's 30-minute upstream timeout settle before CLI retry.
            extra.update(CLAUDE_TRANSPORT_ENV)
            extra.update({"ANTHROPIC_BASE_URL":gateway["url"].removesuffix("/v1"),
                          "ANTHROPIC_AUTH_TOKEN":gateway["token"], "ANTHROPIC_API_KEY":gateway["token"],
                          "CLAUDE_CONFIG_DIR":str(self.workspace/".claude"),"DISABLE_AUTOUPDATER":"1",
                          "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC":"1"})
        else:
            extra.update({"CODEX_HOME":str(self.workspace/".codex"),
                          "C4G_GATEWAY_TOKEN":gateway["token"]})
        return self._launch(argv,cwd=cwd,env=extra,stdin_text=stdin_text,
                            output_mode="stream")

    def launch_codex(self, argv, **kwargs):
        return self.launch_agent("codex", argv, **kwargs)

    def poll(self,handle,max_events=256):
        if type(max_events) is not int or not 1<=max_events<=4096: raise ValueError("max_events must be in 1..4096")
        return self.call("poll",{"handle":handle,"max_events":max_events})
    def terminate(self,handle):
        value=self.call("terminate",{"handle":handle})
        cgroup=self._child_cgroups.get(handle)
        if cgroup:
            self.runtime.native.kill_child_cgroup(self.distro,cgroup)
            self._child_cgroups.pop(handle,None)
        return value
    def close(self):
        was_closed=getattr(self,"closed",False)
        if was_closed and not getattr(self,"_child_cgroups",{}): return
        self.closed=True
        try:
            if not was_closed and getattr(self,"sock",None): self.sock.close()
        finally:
            proc=getattr(self,"process",None)
            if not was_closed and proc and proc.poll() is None:
                proc.terminate()
                try: proc.wait(5)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(5)
            failures=[]
            for handle,cgroup in list(getattr(self,"_child_cgroups",{}).items()):
                try:self.runtime.native.kill_child_cgroup(self.distro,cgroup)
                except Exception as error: failures.append(type(error).__name__)
                else:self._child_cgroups.pop(handle,None)
            if failures:
                raise WslTransportError(f"WSL supervisor child cleanup failed (errors={failures})")
