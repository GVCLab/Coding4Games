"""Standalone unprivileged WSL supervisor. Uses only the Python standard library."""
import base64, codecs, json, os, signal, socket, struct, subprocess, sys, tempfile, time, uuid
from pathlib import Path

MAX_MESSAGE=96*1024*1024; MAX_BYTES=64*1024*1024; MAX_FILES=4096; MAX_OUTPUT=8*1024*1024

def recv(sock):
    def exact(n):
        out=b""
        while len(out)<n:
            chunk=sock.recv(n-len(out))
            if not chunk: raise EOFError()
            out+=chunk
        return out
    n=struct.unpack("!I",exact(4))[0]
    if not 0<n<=MAX_MESSAGE: raise ValueError("invalid frame")
    value=json.loads(exact(n))
    if not isinstance(value,dict): raise ValueError("object required")
    return value
def send(sock,value):
    raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
    if not 0<len(raw)<=MAX_MESSAGE: raise ValueError("invalid frame")
    sock.sendall(struct.pack("!I",len(raw))+raw)

def safe_name(name):
    if not isinstance(name,str) or not name or len(name)>240 or "\\" in name: raise ValueError("unsafe filename")
    p=Path(name)
    if p.is_absolute() or any(x in ("",".","..") for x in p.parts): raise ValueError("unsafe filename")
    return name
def decode(files):
    if not isinstance(files,dict) or not 0<len(files)<=MAX_FILES: raise ValueError("invalid bundle")
    out={}; seen=set(); total=0
    for name,value in files.items():
        safe_name(name); folded=name.casefold()
        if folded in seen or not isinstance(value,str): raise ValueError("filename collision")
        data=base64.b64decode(value,validate=True); total+=len(data)
        if total>MAX_BYTES: raise ValueError("bundle too large")
        seen.add(folded); out[name]=data
    for name in seen:
        parts=name.split("/")
        if any("/".join(parts[:i]).casefold() in seen for i in range(1,len(parts))): raise ValueError("path collision")
    return out

class Child:
    def __init__(self,proc,output_mode="bounded"):
        self.proc=proc; self.bytes=0; self.eof=set(); self.overflow=False
        self.output_mode=output_mode
        self.decoders={key:codecs.getincrementaldecoder("utf-8")("replace") for key in ("stdout","stderr")}

def under(root,value):
    raw=Path(value) if Path(value).is_absolute() else root/Path(value)
    try: relative=raw.relative_to(root)
    except ValueError: raise ValueError("path escapes workspace") from None
    current=root
    for part in relative.parts:
        current=current/part
        if current.is_symlink(): raise ValueError("links are not supported")
    resolved=raw.resolve()
    if resolved != root and root not in resolved.parents: raise ValueError("path escapes workspace")
    return resolved

def main():
    port,workspace=sys.argv[1:]; token=sys.stdin.readline().strip(); sys.stdin.close()
    root=Path(workspace); root.mkdir(parents=True,mode=0o700,exist_ok=True); root=root.resolve()
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError("unsafe workspace ownership or mode")
    children={}
    with socket.create_connection(("127.0.0.1",int(port)),timeout=30) as sock:
        # The connect timeout bounds startup only. Runs can legitimately spend
        # minutes in the game between host commands.
        sock.settimeout(None)
        desktop={path:Path(path).exists() for path in ("/dev/dxg","/mnt/wslg","/tmp/.X11-unix")}
        devices={"null":Path("/dev/null").is_char_device(),"zero":Path("/dev/zero").is_char_device(),
                 "random":Path("/dev/random").is_char_device(),"urandom":Path("/dev/urandom").is_char_device(),
                 "pts":Path("/dev/pts").is_mount(),"shm":Path("/dev/shm").is_mount()}
        resolver_target=str(Path("/etc/resolv.conf").resolve())
        resolver_scope=("private_recreated" if resolver_target.startswith(("/mnt/","/run/","/tmp/"))
                        else "unmasked_retained")
        resolver={"available":Path("/etc/resolv.conf").is_file() and Path("/etc/resolv.conf").stat().st_size>0,
                  "target":resolver_target,"scope":resolver_scope,
                  "mode":(oct(Path(resolver_target).stat().st_mode & 0o777)
                          if resolver_scope=="private_recreated" else None)}
        send(sock,{"token":token,"uid":os.getuid(),"pid":os.getpid(),
                   "mount_namespace":os.readlink("/proc/self/ns/mnt"),"desktop_entrypoints":desktop,
                   "essential_devices":devices,"resolver":resolver,
                   "capabilities":["stdin_text_v1","process_group_v1","bounded_output_v1","streamed_output_v1","private_mount_namespace_v1"]})
        while True:
            try: request=recv(sock)
            except EOFError: break
            try:
                op=request.get("op"); args=request.get("args",{})
                if op in {"stage", "refresh_connection"}:
                    files=decode(args["files"]); name=safe_name(args.get("name","artifact")); target=under(root,name)
                    refresh=op=="refresh_connection"
                    if refresh:
                        allowed={"mcp":{"client.py","c4g_client.py"},
                                 ".claude":{"mcp.json","settings.json"},
                                 ".codex":{"config.toml"}}
                        if name not in allowed or not set(files)<=allowed[name]:
                            raise ValueError("only scoped connection files can be refreshed")
                        if any(child.proc.poll() is None for child in children.values()):
                            raise ValueError("cannot refresh connections beneath a live child")
                    elif target.exists(): raise ValueError("stage target already exists")
                    target.mkdir(mode=0o700,parents=True,exist_ok=refresh)
                    paths={rel:under(target,rel) for rel in files}
                    for rel,data in files.items():
                        path=paths[rel]
                        path.parent.mkdir(parents=True,exist_ok=True)
                        if refresh:
                            temporary=None
                            try:
                                with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as stream:
                                    temporary=Path(stream.name); stream.write(data); stream.flush(); os.fsync(stream.fileno())
                                temporary.chmod(0o600); temporary.replace(path)
                            finally:
                                if temporary is not None and temporary.exists(): temporary.unlink()
                        else: path.write_bytes(data); path.chmod(0o600)
                    value={"path":str(target)}
                elif op=="read_tree":
                    target=under(root,args["path"])
                    result={}; total=0
                    for path in target.rglob("*"):
                        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                            relative=path.relative_to(target).as_posix()
                            raise ValueError(f"special file forbidden: {relative}; save a dedicated source directory without CLI/runtime files")
                        if path.is_file():
                            data=path.read_bytes(); total+=len(data)
                            if len(result)>=MAX_FILES or total>MAX_BYTES: raise ValueError("tree too large")
                            result[path.relative_to(target).as_posix()]=base64.b64encode(data).decode()
                    value={"files":result}
                elif op=="read_file_chunk":
                    target=under(root,args["path"])
                    offset,size=args["offset"],args["size"]
                    if (type(offset) is not int or offset<0 or type(size) is not int
                            or not 1<=size<=1024*1024 or not target.is_file()):
                        raise ValueError("invalid artifact read")
                    with target.open("rb") as source:
                        source.seek(offset); data=source.read(size)
                    value={"bytes":target.stat().st_size,"data":base64.b64encode(data).decode("ascii")}
                elif op=="write_file_chunk":
                    target=under(root,args["path"]); offset=args["offset"]
                    data=base64.b64decode(args["data"],validate=True)
                    if (type(offset) is not int or offset<0 or not 0<len(data)<=1024*1024
                            or offset+len(data)>2*1024**3):
                        raise ValueError("invalid artifact write")
                    target.parent.mkdir(parents=True,exist_ok=True)
                    if offset and (not target.is_file() or target.stat().st_size!=offset):
                        raise ValueError("artifact write offset mismatch")
                    with target.open("xb" if offset==0 else "ab") as output: output.write(data)
                    value={"bytes":offset+len(data)}
                elif op=="launch":
                    argv=args["argv"]; cwd=under(root,args["cwd"])
                    output_mode=args.get("output_mode","bounded")
                    if output_mode not in ("bounded","stream"): raise ValueError("invalid output mode")
                    env={"HOME":str(root),"PATH":"/opt/c4g-tools:/usr/local/bin:/usr/bin:/bin","LANG":"C.UTF-8","LC_ALL":"C.UTF-8"}
                    supplied=args.get("env",{})
                    if not isinstance(supplied,dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in supplied.items()): raise ValueError("invalid env")
                    env.update(supplied); text=args.get("stdin_text")
                    wrapped=["/bin/sh","-c",'kill -STOP $$; exec "$@"',Path(argv[0]).name,*argv]
                    stdin_handle=None; stdin_path=None
                    if text is not None:
                        fd,stdin_path=tempfile.mkstemp(prefix=".stdin-",dir=root); os.fchmod(fd,0o600)
                        with os.fdopen(fd,"wb") as secret: secret.write(text.encode())
                        stdin_handle=open(stdin_path,"rb")
                    proc=subprocess.Popen(wrapped,cwd=cwd,env=env,stdin=stdin_handle or subprocess.DEVNULL,
                                          stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
                    if stdin_handle: stdin_handle.close(); os.unlink(stdin_path)
                    deadline=time.monotonic()+5
                    while time.monotonic()<deadline:
                        try: state=Path(f"/proc/{proc.pid}/stat").read_text().split()[2]
                        except FileNotFoundError: state=""
                        if state in ("T","t"): break
                        if proc.poll() is not None: raise RuntimeError("child exited before verification")
                        time.sleep(.005)
                    else: os.killpg(proc.pid,signal.SIGKILL); raise RuntimeError("child did not suspend")
                    os.set_blocking(proc.stdout.fileno(),False); os.set_blocking(proc.stderr.fileno(),False)
                    handle=uuid.uuid4().hex; children[handle]=Child(proc,output_mode); value={"handle":handle,"pid":proc.pid,"running":True,"suspended":True}
                elif op=="resume":
                    child=children[args["handle"]]; os.killpg(child.proc.pid,signal.SIGCONT); value={"resumed":True}
                elif op=="poll":
                    child=children[args["handle"]]; events=[]; poll_bytes=0
                    for stream,pipe in (("stdout",child.proc.stdout),("stderr",child.proc.stderr)):
                        while len(events)<args["max_events"]:
                            # Managed CLIs emit image-bearing events throughout a
                            # run. Bound each drained response and let pipe
                            # backpressure defer more bytes to the next poll.
                            # Submitted programmes retain the cumulative fuse.
                            remaining=MAX_OUTPUT-(poll_bytes if child.output_mode=="stream" else child.bytes)
                            if child.output_mode=="stream" and remaining==0: break
                            try: data=os.read(pipe.fileno(),min(65536,remaining) if remaining else 1)
                            except BlockingIOError: break
                            if not data:
                                child.eof.add(stream); tail=child.decoders[stream].decode(b"",final=True)
                                if tail: events.append({"stream":stream,"data":tail,"partial":True})
                                break
                            if remaining==0:
                                child.overflow=True
                                try: os.killpg(child.proc.pid,signal.SIGKILL)
                                except ProcessLookupError: pass
                                break
                            child.bytes+=len(data); poll_bytes+=len(data)
                            decoded=child.decoders[stream].decode(data,final=False)
                            if decoded: events.append({"stream":stream,"data":decoded,"partial":not decoded.endswith("\n")})
                    code=child.proc.poll(); complete=code is not None and len(child.eof)==2
                    value={"running":code is None,"exit_code":code,"returncode":code,"events":events,
                           "events_pending":0,"output_complete":complete,"output_overflow":child.overflow,
                           "output_mode":child.output_mode,"output_bytes":child.bytes,
                           "fatal_error":"output_overflow" if child.overflow else None}
                elif op=="terminate":
                    child=children[args["handle"]]
                    try: os.killpg(child.proc.pid,signal.SIGTERM)
                    except ProcessLookupError: pass
                    try: child.proc.wait(5)
                    except subprocess.TimeoutExpired:
                        try: os.killpg(child.proc.pid,signal.SIGKILL)
                        except ProcessLookupError: pass
                        child.proc.wait(5)
                    value={"terminated":True,"exit_code":child.proc.returncode}
                else: raise ValueError("unknown operation")
                send(sock,{"ok":True,"value":value})
            except Exception as exc: send(sock,{"ok":False,"error":f"{type(exc).__name__}: {exc}"[:4000]})
    for child in children.values():
        if child.proc.poll() is None:
            try: os.killpg(child.proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
if __name__=="__main__": main()
