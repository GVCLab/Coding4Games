from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath

ROLES = ("development", "final_test")
_PORT_NAMES = {"development_control", "development_game", "final_test_control", "final_test_game", "gateway"}


class WslProfile:
    backend = "wsl2"
    execution_platform = "linux"
    development_tools = None

    def __init__(self, data):
        required = {"format", "id", "root", "baseline", "baseline_sha256", "user", "uid", "python", "agents", "ports"}
        if not isinstance(data, dict) or set(data) != required or data.get("format") != "c4g-wsl2-v1":
            raise ValueError("unsupported or incomplete WSL2 profile")
        if not isinstance(data["id"], str) or re.fullmatch(r"[0-9a-f]{12}", data["id"]) is None:
            raise ValueError("invalid profile id")
        self.id = data["id"]
        self.root = Path(data["root"]).resolve()
        self.baseline = Path(data["baseline"]).resolve()
        if self.root.name.casefold() != f"runtime-{self.id}":
            raise ValueError("invalid profile root name")
        if data["user"] != "c4gprobe" or data["uid"] != 1000:
            raise ValueError("WSL runtime identity must be c4gprobe uid 1000")
        self.user, self.uid = data["user"], data["uid"]
        digest = data["baseline_sha256"]
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid baseline hash")
        self.baseline_sha256 = digest
        self.python = self._linux_path(data["python"], "python")
        agents = data["agents"]
        if not isinstance(agents, dict) or not agents or not set(agents) <= {"claude", "codex"}:
            raise ValueError("WSL2 agents must be a nonempty selection of claude and codex")
        self.agents = {name: self._linux_path(value, "agent") for name, value in agents.items()}
        ports = data["ports"]
        if (not isinstance(ports, dict) or set(ports) != _PORT_NAMES or
                any(type(v) is not int or not 1024 <= v <= 65535 for v in ports.values()) or
                len(set(ports.values())) != 5):
            raise ValueError("five distinct unprivileged ports required")
        self.ports = dict(ports)

    @staticmethod
    def _linux_path(value, label):
        if not isinstance(value, str): raise ValueError(f"invalid {label} path")
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or "\\" in value:
            raise ValueError(f"invalid {label} path")
        return path

    @classmethod
    def load(cls, path):
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def agent(self, name):
        if name not in self.agents: raise ValueError(f"agent CLI is not configured: {name}")
        return self.agents[name]

    @property
    def codex(self):
        return self.agent("codex")

    def control_port(self, role):
        if role not in ROLES: raise ValueError("invalid WSL2 role")
        return self.ports[f"{role}_control"]

    def box(self, role):
        if role not in ROLES: raise ValueError("invalid WSL2 role")
        return f"C4G-WSL2-Run-{self.id}-{role}"
