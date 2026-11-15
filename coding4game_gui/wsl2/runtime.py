from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
import msvcrt
from pathlib import Path

from .native import WslNative
from .profile import ROLES
from .supervisor import WslSupervisor


class WslIsolationError(RuntimeError): pass


class WslRuntime:
    def __init__(self, profile, evidence_dir=None, *, native=None, supervisor_factory=None):
        self.profile = profile; self.evidence_dir = Path(evidence_dir).resolve() if evidence_dir else None
        self.native = native or WslNative(); self.supervisor_factory = supervisor_factory or WslSupervisor
        self.archives = []; self._started = {}; self._clones = {}; self._cgroups = {}; self._lock = None
        self._topology = None; self._reference_namespaces = {}; self._run_namespace=None
        self._role_namespaces = {}
        self._restored_workspaces = {}
        self._recovery_evidence = {}
        self._agent_upgrades = {}

    def _lifecycle(self,name,**values):
        root=self.profile.root/"clone_records"; root.mkdir(parents=True,exist_ok=True)
        path=root/f"{name}.json"; current={}
        if path.is_file(): current=json.loads(path.read_text(encoding="utf-8"))
        current.update(values); current.update({"distro":name,"profile_id":self.profile.id,"updated_unix":time.time()})
        temporary=path.with_suffix(".tmp")
        temporary.write_text(json.dumps(current,sort_keys=True,indent=2),encoding="utf-8"); temporary.replace(path)
        return current

    def _check_baseline(self):
        if not self.profile.baseline.is_file(): raise WslIsolationError("baseline archive is missing")
        digest = self._sha256(self.profile.baseline)
        if digest != self.profile.baseline_sha256: raise WslIsolationError("baseline archive hash mismatch")

    @staticmethod
    def _sha256(path):
        digest=hashlib.sha256()
        with Path(path).open("rb") as source:
            for block in iter(lambda:source.read(1024*1024),b""): digest.update(block)
        return digest.hexdigest()

    def preflight(self, protected=()):
        self._check_baseline()
        protected = [Path(p).resolve() for p in protected]
        if any(p == self.profile.baseline or self.profile.baseline in p.parents for p in protected):
            raise WslIsolationError("protected path overlaps WSL baseline")
        state = self.native.verify_host()
        distros=self.native.list_distros()
        for reference in ("C4G-WSL2-Probe-20261013","C4G-WSL2-Base-20261013"):
            if reference in distros: self._reference_namespaces[reference]=self.native.network_namespace(reference)
        namespaces=set(self._reference_namespaces.values())
        if len(namespaces)>1: raise WslIsolationError("trusted WSL references disagree on network topology")
        self._topology=("shared_wsl_network_namespace" if namespaces else "reference_unavailable")
        state["network_topology"]={"classification":self._topology,"references":dict(self._reference_namespaces)}
        prefix=f"C4G-WSL2-Run-{self.profile.id}-"
        state["preserved_clones"]=sorted(name for name in distros if name.startswith(prefix))
        records=[]
        for path in sorted((self.profile.root/"clone_records").glob("*.json")) if (self.profile.root/"clone_records").is_dir() else ():
            try: records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError,ValueError): records.append({"path":str(path),"status":"unreadable"})
        state["clone_records"]=records
        return {"backend":"wsl2", "profile_id":self.profile.id, "execution_platform":"linux",
                "host":state, "security_claim":"host and guest checks; not proof of complete isolation"}

    def __enter__(self):
        self.profile.root.mkdir(parents=True, exist_ok=True)
        lock = self.profile.root / "run.lock"; self._lock=lock.open("a+b"); self._lock.seek(0)
        try: msvcrt.locking(self._lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self._lock.close(); self._lock=None; raise WslIsolationError("WSL2 profile already in use") from None
        self._lock.seek(0); self._lock.truncate(); self._lock.write(str(os.getpid()).encode()); self._lock.flush()
        return self

    def start(self, role, *, recovery=None, agent_upgrade=None):
        if role not in ROLES: raise ValueError("invalid WSL2 role")
        if self._lock is None: raise WslIsolationError("runtime context is not active")
        if agent_upgrade is not None:
            from .agent_upgrade import VerifiedAgentUpgrade
            if role != "development" or recovery is None or not isinstance(agent_upgrade, VerifiedAgentUpgrade):
                raise WslIsolationError("agent upgrades require a verified development recovery authorization")
        if role in self._started:
            if recovery is not None: raise WslIsolationError("cannot restore beneath a live supervisor")
            return self._started[role]
        workspace = None
        if recovery is not None:
            if role != "development": raise WslIsolationError("final tests require a clean workspace")
            from .recovery import verify_workspace_archive
            workspace = verify_workspace_archive(recovery, self.profile)
        self._check_baseline()
        reference="C4G-WSL2-Probe-20261013"
        for _ in range(32):
            name = f"C4G-WSL2-Run-{self.profile.id}-{role}-{secrets.token_hex(6)}"
            if reference not in self.native.list_distros() or self.native.uid_available(reference,self.native.candidate_uid(name)):
                break
        else: raise WslIsolationError("could not allocate an unclaimed WSL run UID")
        install = self.profile.root / "clones" / name
        if name in self.native.list_distros(): raise WslIsolationError("generated WSL clone already exists")
        self.native.import_distro(name, install, self.profile.baseline)
        self._clones[role] = (name, install)
        self._lifecycle(name,role=role,install=str(install),baseline_sha256=self.profile.baseline_sha256,status="imported")
        try:
            if agent_upgrade is not None:
                from .agent_upgrade import INSTALL_AGENT_UPGRADE
                result = self.native.exec(name, [str(self.profile.python), "-I", "-B", "-c", INSTALL_AGENT_UPGRADE],
                                          input=agent_upgrade.install_input(), timeout=120)
                receipt = json.loads(result.stdout)
                record = agent_upgrade.record
                if any(receipt.get(key) != record[key] for key in ("agent", "version", "sha256", "bytes", "executable")):
                    raise WslIsolationError("agent upgrade installation receipt mismatch")
                self._agent_upgrades[role] = receipt
                self._lifecycle(name, status="agent_upgraded", agent_upgrade=receipt)
            clone_namespace=self.native.network_namespace(name)
            if self._reference_namespaces and clone_namespace not in set(self._reference_namespaces.values()):
                self._lifecycle(name,status="topology_mismatch",network_namespace=clone_namespace)
                raise WslIsolationError("WSL clone network topology changed after preflight")
            if self._run_namespace is not None and clone_namespace != self._run_namespace:
                self._lifecycle(name,status="topology_mismatch",network_namespace=clone_namespace)
                raise WslIsolationError("WSL roles do not share the recorded network topology")
            self._run_namespace=clone_namespace
            self._role_namespaces[role]=clone_namespace
            if self._topology is None:
                self._topology="single_run_namespace_observed"
            isolation=self.native.configure(name, role=role, control_port=self.profile.control_port(role),
                                            game_port=self.profile.ports[f"{role}_game"], gateway_port=self.profile.ports["gateway"],
                                            agents=self.profile.agents)
            self._cgroups[role]=isolation
            verification=self.native.verify(name, role=role, control_port=self.profile.control_port(role),
                               game_port=self.profile.ports[f"{role}_game"], gateway_port=self.profile.ports["gateway"],
                               python=self.profile.python, agents=self.profile.agents)
            isolation["verification"]=verification
            self._lifecycle(name,status="verified",network_namespace=clone_namespace,network_topology=self._topology,
                            isolation=verification)
            if workspace is not None:
                from .recovery import RESTORE_WORKSPACE
                restored = self.native.exec(name, [str(self.profile.python), "-I", "-B", "-c", RESTORE_WORKSPACE],
                                            input=workspace.restore_input(isolation["uid"]), timeout=120)
                receipt = json.loads(restored.stdout)
                if (receipt.get("workspace") != str(workspace.workspace)
                        or receipt.get("verified_entries") != len(workspace.entries)
                        or receipt.get("verified_files") != sum(
                            value["kind"] in {"file", "hardlink"} for value in workspace.entries.values())):
                    raise WslIsolationError("restored workspace verification receipt mismatch")
                self._restored_workspaces[role] = workspace.workspace
                self._recovery_evidence[role] = receipt | {"archive_sha256": workspace.archive_sha256}
                self._lifecycle(name,status="workspace_restored",recovery=self._recovery_evidence[role])
            supervisor = self.supervisor_factory(self, role, name)
            mount_isolation=getattr(supervisor,"mount_isolation",None)
            if mount_isolation is not None: verification["mount_isolation"]=dict(mount_isolation)
            self._started[role] = supervisor
            self._lifecycle(name,status="supervisor_started",isolation=verification)
            return supervisor
        except BaseException as error:
            # Preserve the failed clone for evidence and diagnosis.
            record=self._lifecycle(name,error_type=type(error).__name__)
            if record.get("status") != "topology_mismatch": self._lifecycle(name,status="failed")
            raise

    def role_evidence(self, role):
        """Return non-secret verified identity and isolation evidence for one role."""
        if role not in self._started or role not in self._cgroups or role not in self._role_namespaces:
            raise WslIsolationError("WSL role has not completed verified startup")
        name, _ = self._clones[role]
        verification = self._cgroups[role].get("verification")
        if not isinstance(verification, dict):
            raise WslIsolationError("WSL role verification evidence is unavailable")
        result={"role":role, "distro":name, "profile_id":self.profile.id,
                "baseline_sha256":self.profile.baseline_sha256,
                "network_namespace":self._role_namespaces[role],
                "network_topology":self._topology,
                "isolation":dict(verification)}
        readiness=getattr(self._started[role],"control_listener_readiness",None)
        if isinstance(readiness,dict): result["control_listener"]=dict(readiness)
        if role in self._recovery_evidence: result["recovery"] = dict(self._recovery_evidence[role])
        if role in self._agent_upgrades: result["agent_upgrade"] = dict(self._agent_upgrades[role])
        return result

    def _export(self, role, name):
        root = (self.evidence_dir / "wsl2_exports" if self.evidence_dir else self.profile.root / "archives")
        root.mkdir(parents=True, exist_ok=True)
        archive = root / f"{name}.tar"
        probe = ("if [ -L /home/c4gprobe/run ]; then printf unsafe; "
                 "elif find /home/c4gprobe/run -mindepth 2 -maxdepth 2 -name .c4g-dependencies -type l -print -quit 2>/dev/null | grep -q .; then printf unsafe; "
                 "elif [ -d /home/c4gprobe/run ]; then printf directory; "
                 "elif [ -e /home/c4gprobe/run ]; then printf unsafe; "
                 "else printf absent; fi")
        workspace_state = self.native.exec(
            name, ["/bin/sh", "-c", probe], user="root").stdout.decode().strip()
        if workspace_state not in {"directory", "absent"}:
            raise WslIsolationError("WSL archive workspace is not a safe directory")
        if workspace_state == "absent" and role in self._started:
            raise WslIsolationError("started WSL archive workspace disappeared")
        argv = ["/bin/tar", "--numeric-owner", "--xattrs"]
        if workspace_state == "directory":
            argv += ["--exclude=run/*/.c4g-dependencies", "-C", "/home/c4gprobe", "-cf", "-", "run"]
        else:
            # Verification can fail before a supervisor creates any workspace.
            argv += ["-cf", "-", "--files-from", "/dev/null"]
        with archive.open("wb") as output:
            self.native.exec(name, argv, user="root", timeout=120, stdout=output)
        record = {"role":role, "distro":name, "path":str(archive), "bytes":archive.stat().st_size,
                  "sha256":self._sha256(archive),"network_topology":self._topology,
                  "isolation":self._cgroups.get(role,{}).get("verification"),
                  "workspace_state":workspace_state}
        record.update(profile_id=self.profile.id, baseline_sha256=self.profile.baseline_sha256,
                      workspace=(str(self._started[role].workspace)
                                 if role in self._started and hasattr(self._started[role], "workspace") else None))
        record["excluded_reconstructable_paths"] = ["run/*/.c4g-dependencies"]
        if role in self._agent_upgrades: record["agent_upgrade"] = dict(self._agent_upgrades[role])
        manifest = archive.with_suffix(".json")
        manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        self.archives.append(record); return record

    def __exit__(self, *exc):
        errors=[]
        for supervisor in reversed(list(self._started.values())):
            try: supervisor.close()
            except Exception as error: errors.append(error)
        for role,(name, install) in list(self._clones.items()):
            exported=False
            try:
                self._export(role,name); exported=True
                self._lifecycle(name,status="exported")
                key="c4g"+name.rsplit("-",1)[-1]
                self.native.teardown(name,self._cgroups.get(
                    role,{"table":key,"cgroup":f"/sys/fs/cgroup/{key}"}))
                self.native.terminate(name)
                self.native.unregister(name)
                self._lifecycle(name,status="unregistered")
                if install.exists(): install.rmdir()
            except Exception as error: errors.append(error)
            if not exported: continue
        if self._lock is not None:
            self._lock.close(); self._lock=None
        if errors: raise WslIsolationError(f"WSL2 cleanup failed; failed clones were preserved: {errors}")
