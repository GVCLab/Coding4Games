"""No-model acceptance probe for abnormal WSL child-tree cleanup.

Preview is the default. Execution creates one fresh development clone, launches
one probe-owned parent and grandchild in a verified nested cgroup, lets the
parent exit abnormally, and verifies that supervisor shutdown removes the
surviving grandchild before WslRuntime exports and unregisters the clone.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from coding4game_gui.wsl2 import WslProfile, WslRuntime


SCHEMA = 1
ROLE = "development"
ABNORMAL_EXIT = 23


def _probe_source() -> str:
    return r'''import json, os, subprocess, sys, time
child=subprocess.Popen(
    [sys.executable,"-c","import time; time.sleep(300)"],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
    start_new_session=True,close_fds=True)
print(json.dumps({"parent_pid":os.getpid(),"grandchild_pid":child.pid}),flush=True)
time.sleep(.1)
os._exit(23)
'''


def _collect_abnormal(supervisor, handle: str, timeout: float = 10) -> tuple[dict, dict]:
    deadline=time.monotonic()+timeout; stdout=[]; stderr=[]; final=None
    while time.monotonic() < deadline:
        state=supervisor.poll(handle)
        for event in state["events"]:
            (stdout if event["stream"] == "stdout" else stderr).append(event["data"])
        if not state["running"] and state["output_complete"]:
            final=state; break
        time.sleep(.05)
    if final is None:
        supervisor.terminate(handle)
        raise TimeoutError("abnormal cleanup canary child did not settle")
    if final["returncode"] != ABNORMAL_EXIT:
        raise RuntimeError(f"probe exit was {final['returncode']}, expected {ABNORMAL_EXIT}: {''.join(stderr)[:500]}")
    lines=[line for line in "".join(stdout).splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("probe did not report exactly one owned process record")
    return json.loads(lines[0]), final


def _scope_pids(native, distro: str, cgroup: str) -> list[int]:
    """Read only the exact, runtime-created child cgroup membership."""
    if (not isinstance(cgroup,str) or not cgroup.startswith("/sys/fs/cgroup/c4g") or
            "/child-" not in cgroup or any(ch.isspace() for ch in cgroup)):
        raise ValueError("unsafe nested cgroup path")
    raw=native.exec(distro,["/bin/cat",f"{cgroup}/cgroup.procs"],user="root",timeout=5).stdout.decode()
    try: pids=[int(line) for line in raw.splitlines() if line]
    except ValueError as error: raise RuntimeError("invalid nested cgroup membership") from error
    if any(pid <= 1 for pid in pids): raise RuntimeError("invalid PID in nested cgroup")
    return sorted(pids)


def _scope_removed(native, distro: str, cgroup: str) -> bool:
    if not isinstance(cgroup,str) or not cgroup.startswith("/sys/fs/cgroup/c4g") or "/child-" not in cgroup:
        raise ValueError("unsafe nested cgroup path")
    command='if test ! -e "$1"; then printf removed; else printf present; fi'
    completed=native.exec(distro,["/bin/sh","-c",command,"scope-check",cgroup],user="root",timeout=5)
    return completed.stdout == b"removed"


def preview(profile: WslProfile, out: Path) -> dict:
    return {"schema":SCHEMA,"execute":False,"profile_id":profile.id,"out":str(out.resolve()),
            "role":ROLE,"model_calls":0,"game_started":False,"maximum_probe_wait_seconds":10,
            "note":"add --execute to lock the profile, create one fresh clone, and run the owned process-tree probe"}


def execute(profile: WslProfile, out: Path, *, runtime_factory=WslRuntime) -> dict:
    out=out.resolve()
    if out.exists(): raise ValueError("--out must be a new evidence directory")
    runtime=runtime_factory(profile,evidence_dir=out)
    result={"schema":SCHEMA,"status":"running","profile_id":profile.id,
            "baseline_sha256":profile.baseline_sha256,"role":ROLE,"model_calls":0,"game_started":False,
            "limitations":["This probe covers only its own verified nested cgroup and never enumerates or signals unrelated processes.",
                           "It does not replace selected-candidate development or three independent final tests."]}
    evidence=out/"result.json"
    def save(): evidence.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    try:
        with runtime:
            out.mkdir(parents=True); save()
            result["preflight"]=runtime.preflight(protected=(out,)); save()
            supervisor=runtime.start(ROLE)
            role_evidence=runtime.role_evidence(ROLE)
            distro=role_evidence["distro"]
            staged=supervisor.stage({"abnormal_parent.py":_probe_source().encode()},name="abnormal-cleanup")
            launched=supervisor.launch([str(profile.python),"-I","-B",str(staged/"abnormal_parent.py")],cwd=staged)
            handle=launched.get("handle")
            if not isinstance(handle,str): raise RuntimeError("WSL supervisor launch returned no process handle")
            child_cgroup=supervisor._child_cgroups.get(handle)
            if not isinstance(child_cgroup,str): raise RuntimeError("verified nested child cgroup is unavailable")
            owned, final=_collect_abnormal(supervisor,handle)
            if owned.get("parent_pid") != launched.get("pid"):
                raise RuntimeError("probe parent identity disagrees with verified launch identity")
            grandchild_pid=owned.get("grandchild_pid")
            identity=runtime.native.verify_child(
                distro,grandchild_pid,role_evidence["isolation"]["uid"],child_cgroup,
                supervisor.mount_isolation["mount_namespace"])
            members=_scope_pids(runtime.native,distro,child_cgroup)
            if members != [grandchild_pid]:
                raise RuntimeError("nested cgroup contains a process outside the one surviving probe-owned grandchild")
            result["before_supervisor_close"]={"runtime":role_evidence,"launch_pid":launched["pid"],
                "grandchild_identity":identity,"nested_cgroup":child_cgroup,"nested_cgroup_pids":members,
                "parent_returncode":final["returncode"],"parent_output_complete":final["output_complete"]}
            save()
            supervisor.close()
            removed=_scope_removed(runtime.native,distro,child_cgroup)
            if not removed: raise RuntimeError("supervisor close left the owned nested cgroup present")
            result["after_supervisor_close"]={"nested_cgroup_removed":True}; save()
        result["runtime_exports"]=list(runtime.archives)
        if len(runtime.archives) != 1 or runtime.archives[0].get("distro") != distro:
            raise RuntimeError("runtime export evidence is missing")
        if distro in runtime.native.list_distros(): raise RuntimeError("fresh canary clone remained registered")
        lifecycle_path=profile.root/"clone_records"/f"{distro}.json"
        lifecycle=json.loads(lifecycle_path.read_text(encoding="utf-8"))
        if lifecycle.get("status") != "unregistered": raise RuntimeError("clone lifecycle lacks unregister evidence")
        result["cleanup"]={"archive":runtime.archives[0],"clone_registered":False,
                           "lifecycle_record":str(lifecycle_path),"lifecycle_status":"unregistered"}
        result["status"]="passed_with_documented_limitations"
    except BaseException as error:
        result.update(status="failed",error_type=type(error).__name__,error=str(error)[:1000])
        if getattr(runtime,"archives",None): result["runtime_exports"]=list(runtime.archives)
        if out.is_dir(): save()
        raise
    save(); return result


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",type=Path,required=True); parser.add_argument("--out",type=Path,required=True)
    parser.add_argument("--execute",action="store_true"); args=parser.parse_args()
    profile=WslProfile.load(args.profile)
    if not args.execute:
        print(json.dumps(preview(profile,args.out),indent=2)); return 0
    result=execute(profile,args.out)
    print(json.dumps({"status":result["status"],"evidence":str(args.out.resolve()/"result.json")},indent=2))
    return 0 if result["status"].startswith("passed") else 1


if __name__ == "__main__": raise SystemExit(main())
