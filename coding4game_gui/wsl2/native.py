from __future__ import annotations

import ipaddress
import json
import re
import shlex
import subprocess
from pathlib import Path


_PRIVATE_IPV4 = frozenset({"10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
                           "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16"})
_PRIVATE_IPV6 = frozenset({"::1", "fc00::/7", "fe80::/10"})
_RUN_TABLE = re.compile(r"c4g[0-9a-f]{12}\Z")
_RUN_CGROUP = re.compile(r"/sys/fs/cgroup/(c4g[0-9a-f]{12})\Z")


def _nft_value(value):
    if isinstance(value, dict) and set(value) == {"prefix"}:
        prefix=value["prefix"]
        if not isinstance(prefix,dict) or set(prefix) != {"addr","len"}:
            raise RuntimeError("unexpected WSL nftables prefix")
        try: return str(ipaddress.ip_network(f'{prefix["addr"]}/{prefix["len"]}',strict=True))
        except (TypeError,ValueError) as exc: raise RuntimeError("invalid WSL nftables prefix") from exc
    return value


def _nft_match(expression):
    """Return a small semantic form for the match expressions we generate."""
    if not isinstance(expression, dict) or set(expression) != {"match"}:
        raise RuntimeError("unexpected WSL nftables expression")
    match = expression["match"]
    if not isinstance(match, dict) or set(match) != {"op", "left", "right"} or match["op"] != "==":
        raise RuntimeError("unexpected WSL nftables match")
    left = match["left"]
    if isinstance(left, dict) and set(left) == {"meta"} and set(left["meta"]) == {"key"}:
        key = ("meta", left["meta"]["key"])
    elif isinstance(left, dict) and set(left) == {"payload"} and set(left["payload"]) == {"protocol", "field"}:
        key = ("payload", left["payload"]["protocol"], left["payload"]["field"])
    else:
        raise RuntimeError("unexpected WSL nftables match operand")
    right = match["right"]
    if isinstance(right, dict) and set(right) == {"set"} and isinstance(right["set"], list):
        right = frozenset(_nft_value(value) for value in right["set"])
    else:
        right = _nft_value(right)
    return key, right


def _nft_rule(rule):
    if not isinstance(rule, dict) or not {"family", "table", "chain", "expr"} <= set(rule):
        raise RuntimeError("invalid WSL nftables rule")
    if set(rule) - {"family", "table", "chain", "expr", "handle"}:
        raise RuntimeError("unexpected WSL nftables rule metadata")
    if not isinstance(rule["expr"], list):
        raise RuntimeError("invalid WSL nftables expressions")
    result = []
    for expression in rule["expr"]:
        if isinstance(expression, dict) and set(expression) == {"accept"} and expression["accept"] is None:
            result.append(("verdict", "accept"))
        elif isinstance(expression, dict) and set(expression) == {"drop"} and expression["drop"] is None:
            result.append(("verdict", "drop"))
        else:
            result.append(_nft_match(expression))
    return tuple(result)


def _verify_nft_ruleset(raw, *, table, role, uid, allowed_ports, dns=None):
    """Fail closed unless nft's JSON describes exactly the policy we install."""
    try:
        document = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid WSL nftables JSON") from exc
    items = document.get("nftables") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("invalid WSL nftables JSON")
    objects = [item for item in items if not (isinstance(item, dict) and set(item) == {"metainfo"})]
    tables = [item["table"] for item in objects if isinstance(item, dict) and set(item) == {"table"}]
    chains = [item["chain"] for item in objects if isinstance(item, dict) and set(item) == {"chain"}]
    rules = [item["rule"] for item in objects if isinstance(item, dict) and set(item) == {"rule"}]
    if len(tables) != 1 or len(chains) != 1 or len(objects) != 2 + len(rules):
        raise RuntimeError("unexpected WSL nftables objects")
    table_object = tables[0]
    if (not isinstance(table_object, dict) or set(table_object) - {"family", "name", "handle"} or
            table_object.get("family") != "inet" or table_object.get("name") != table):
        raise RuntimeError("unexpected WSL nftables table")
    chain = chains[0]
    if (not isinstance(chain, dict) or
            set(chain) - {"family", "table", "name", "handle", "type", "hook", "prio", "policy"} or
            {key: chain.get(key) for key in ("family", "table", "name", "type", "hook", "prio", "policy")} !=
            {"family":"inet", "table":table, "name":"output", "type":"filter", "hook":"output",
             "prio":0, "policy":"accept"}):
        raise RuntimeError("unexpected WSL nftables output chain")
    for rule in rules:
        if rule.get("family") != "inet" or rule.get("table") != table or rule.get("chain") != "output":
            raise RuntimeError("WSL nftables rule escaped expected table")
    ports = frozenset(allowed_ports)
    owner = (("meta", "skuid"), uid)
    ipv4_loopback = (("payload", "ip", "daddr"), "127.0.0.0/8")
    ipv6_loopback = (("payload", "ip6", "daddr"), "::1")
    tcp_ports = (("payload", "tcp", "dport"), ports)
    expected = [
        (owner, ipv4_loopback, tcp_ports, ("verdict", "accept")),
        (owner, ipv6_loopback, tcp_ports, ("verdict", "accept")),
    ]
    if role == "final_test":
        expected.append((owner, ("verdict", "drop")))
    elif role == "development":
        try:
            dns = str(ipaddress.IPv4Address(dns))
        except (ipaddress.AddressValueError, ValueError) as exc:
            raise RuntimeError("invalid WSL resolver address") from exc
        expected.extend([
            (owner, (("payload", "ip", "daddr"), dns), (("payload", "udp", "dport"), 53), ("verdict", "accept")),
            (owner, (("payload", "ip", "daddr"), dns), (("payload", "tcp", "dport"), 53), ("verdict", "accept")),
            (owner, (("payload", "ip", "daddr"), _PRIVATE_IPV4), ("verdict", "drop")),
            (owner, (("payload", "ip6", "daddr"), _PRIVATE_IPV6), ("verdict", "drop")),
        ])
    else:
        raise RuntimeError("unsupported WSL network role")
    if [_nft_rule(rule) for rule in rules] != expected:
        raise RuntimeError("unexpected WSL nftables policy")


class WslNative:
    """Small, argument-list-only wrapper around wsl.exe."""
    def __init__(self, executable="wsl.exe"): self.executable = executable

    def _run(self, args, *, timeout=60, input=None, stdout=subprocess.PIPE):
        kwargs={"check":True,"timeout":timeout,"stdout":stdout,"stderr":subprocess.PIPE,
                "creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
        if input is None: kwargs["stdin"]=subprocess.DEVNULL
        else: kwargs["input"]=input
        return subprocess.run([self.executable, *map(str, args)], **kwargs)

    @staticmethod
    def _text(raw):
        return raw.decode("utf-16-le" if b"\x00" in raw else "utf-8","replace").lstrip("\ufeff")

    def verify_host(self):
        text = self._text(self._run(["--version"]).stdout)
        if not text.strip(): raise RuntimeError("WSL version could not be verified")
        return {"wsl_version": 2, "version_output": text[:1000]}

    def list_distros(self):
        raw = self._text(self._run(["--list", "--quiet"]).stdout)
        return {line.strip().lstrip("\ufeff") for line in raw.splitlines() if line.strip()}

    def import_distro(self, name, install, baseline):
        Path(install).mkdir(parents=True, exist_ok=False)
        self._run(["--import", name, install, baseline, "--version", "2"], timeout=300)

    def network_namespace(self,name):
        return self.exec(name,["/usr/bin/readlink","/proc/self/ns/net"],user="root").stdout.decode().strip()

    def mount_namespace(self,name):
        return self.exec(name,["/usr/bin/readlink","/proc/self/ns/mnt"],user="root").stdout.decode().strip()

    def resolver_target(self,name):
        command="set -eu; test -s /etc/resolv.conf; readlink -f /etc/resolv.conf"
        target=self.exec(name,["/bin/sh","-c",command],user="root").stdout.decode().strip()
        if not target: raise RuntimeError("WSL resolver target is unavailable")
        return target

    @staticmethod
    def candidate_uid(name): return 20000+(int(name.rsplit("-",1)[-1],16)%30000)

    def uid_available(self,name,uid):
        script="ps -eo uid=; nft list ruleset 2>/dev/null || true"
        text=self.exec(name,["/bin/sh","-c",script],user="root").stdout.decode()
        live={int(value) for value in re.findall(r"(?m)^\s*(\d+)\s*$",text)}
        claimed={int(value) for value in re.findall(r"meta skuid (\d+)",text)}
        return uid not in live|claimed

    def exec(self, name, argv, *, user="root", timeout=60, input=None, stdout=subprocess.PIPE):
        return self._run(["--distribution", name, "--user", user, "--exec", *argv], timeout=timeout, input=input, stdout=stdout)

    def configure(self, name, *, role, control_port, game_port, gateway_port, agents=None):
        allowed = [control_port, game_port] + ([gateway_port] if role == "development" else [])
        key="c4g"+name.rsplit("-",1)[-1]
        cgroup=f"/sys/fs/cgroup/{key}"
        run_uid=self.candidate_uid(name)
        if not self.uid_available(name,run_uid): raise RuntimeError("generated WSL run UID is already claimed")
        allowed_text=", ".join(map(str,allowed))
        configured_tools = agents or {"claude": "/opt/c4g-tools/claude"}
        other_tools = {"/opt/c4g-tools/claude", "/opt/c4g-tools/codex-runtime/bin/codex"} - {
            str(path) for path in configured_tools.values()}
        policy = ([f"nft add rule inet {key} output meta skuid $run_uid ip daddr 127.0.0.0/8 tcp dport {{ {allowed_text} }} accept",
                   f"nft add rule inet {key} output meta skuid $run_uid ip6 daddr ::1 tcp dport {{ {allowed_text} }} accept",
                   f"nft add rule inet {key} output meta skuid $run_uid drop"] if role=="final_test" else
                  [f"nft add rule inet {key} output meta skuid $run_uid ip daddr 127.0.0.0/8 tcp dport {{ {allowed_text} }} accept",
                   f"nft add rule inet {key} output meta skuid $run_uid ip6 daddr ::1 tcp dport {{ {allowed_text} }} accept",
                   f"nft add rule inet {key} output meta skuid $run_uid ip daddr $dns udp dport 53 accept",
                   f"nft add rule inet {key} output meta skuid $run_uid ip daddr $dns tcp dport 53 accept",
                   f"nft add rule inet {key} output meta skuid $run_uid ip daddr {{ 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 }} drop",
                   f"nft add rule inet {key} output meta skuid $run_uid ip6 daddr {{ ::1, fc00::/7, fe80::/10 }} drop"])
        script = "\n".join([
            "set -eu", "id c4gprobe >/dev/null", "test \"$(id -u c4gprobe)\" = 1000",
            f"groupmod -g {run_uid} c4gprobe; usermod -u {run_uid} -g {run_uid} c4gprobe; chown -R {run_uid}:{run_uid} /home/c4gprobe",
            *[f"test ! -L {shlex.quote(str(path))}; test -f {shlex.quote(str(path))}; "
              f"chown root:root {shlex.quote(str(path))}; "
              f"chmod {'0700' if role == 'final_test' else '0755'} {shlex.quote(str(path))}"
              for path in configured_tools.values()],
            *[f"if [ -e {shlex.quote(path)} ]; then chown root:root {shlex.quote(path)}; "
              f"chmod {'0700' if role == 'final_test' else '0755'} {shlex.quote(path)}; fi"
              for path in sorted(other_tools)],
            f"mkdir {cgroup}", f"cg=$(stat -c %i {cgroup})", f"run_uid={run_uid}",
            "dns=$(awk '/^nameserver /{print $2; exit}' /etc/resolv.conf)", f"nft add table inet {key}",
            f"nft 'add chain inet {key} output {{ type filter hook output priority 0; policy accept; }}'",
            *policy,
        ])
        self.exec(name, ["/bin/sh", "-c", script])
        return {"cgroup":cgroup,"table":key,"role":role,"allowed_ports":allowed,"uid":run_uid}

    def verify(self, name, *, role, control_port, game_port, gateway_port, python, agents=None):
        key="c4g"+name.rsplit("-",1)[-1]
        uid = int(self.exec(name, ["id", "-u"], user="root").stdout)
        if uid != 0: raise RuntimeError("WSL guest isolation verification failed")
        agent_tools = {}
        for agent, path in (agents or {"claude": "/opt/c4g-tools/claude"}).items():
            tool_mode = self.exec(name, ["stat", "-c", "%a", str(path)], user="root").stdout.decode().strip()
            if tool_mode != ("700" if role=="final_test" else "755"):
                raise RuntimeError(f"unexpected WSL {agent} tool permissions")
            agent_tools[agent] = {"path": str(path), "mode": tool_mode}
        user_uid = int(self.exec(name, ["id", "-u"], user="c4gprobe").stdout)
        expected_uid=20000+(int(name.rsplit("-",1)[-1],16)%30000)
        if user_uid != expected_uid: raise RuntimeError("unexpected WSL runtime user")
        allowed=[control_port,game_port]+([gateway_port] if role=="development" else [])
        dns=(self.exec(name,["/bin/sh","-c","awk '/^nameserver /{print $2; exit}' /etc/resolv.conf"],user="root").stdout.decode().strip()
             if role=="development" else None)
        ruleset=self.exec(name,["/usr/sbin/nft","-j","list","table","inet",key],user="root").stdout.decode()
        _verify_nft_ruleset(ruleset,table=key,role=role,uid=user_uid,allowed_ports=allowed,dns=dns)
        if role == "final_test":
            cgroup=f"/sys/fs/cgroup/{key}"
            code="import socket; s=socket.socket(); s.settimeout(1);\ntry:s.connect(('1.1.1.1',443)); raise SystemExit(9)\nexcept OSError:pass"
            wrapper='cg=$1; uid=$2; shift 2; echo $$ > "$cg/cgroup.procs"; exec setpriv --reuid="$uid" --regid="$uid" --init-groups "$@"'
            self.exec(name,["/bin/sh","-c",wrapper,"canary",cgroup,str(user_uid),"/usr/bin/python3","-c",code],user="root")
            canary="blocked_in_run_cgroup"
        else: canary="development_private_ranges_filtered"
        dependency_code=("import json,cv2,numpy; print(json.dumps({"
                         "'cv2':cv2.__version__,'numpy':numpy.__version__},sort_keys=True))")
        dependency_raw=self.exec(name,[str(python),"-I","-B","-c",dependency_code],user="c4gprobe").stdout.decode()
        try: dependencies=json.loads(dependency_raw)
        except (TypeError,ValueError) as exc: raise RuntimeError("invalid WSL dependency probe output") from exc
        if set(dependencies)!={"cv2","numpy"} or any(not isinstance(value,str) or not value for value in dependencies.values()):
            raise RuntimeError("invalid WSL dependency versions")
        return {"uid": user_uid, "network_canary": canary, "nftables": True,
                "network_scope":"unique_run_uid", "process_scope":"verified_nested_cgroups",
                "python":str(python),"dependencies":dependencies,"agent_tools":agent_tools}

    @staticmethod
    def _final_network_identity(isolation):
        if not isinstance(isolation, dict) or isolation.get("role") != "final_test":
            raise RuntimeError("dependency network transition requires final_test isolation")
        table=isolation.get("table"); cgroup=isolation.get("cgroup")
        uid=isolation.get("uid"); ports=isolation.get("allowed_ports")
        match=_RUN_CGROUP.fullmatch(cgroup) if isinstance(cgroup,str) else None
        if (not isinstance(table,str) or not _RUN_TABLE.fullmatch(table) or
                match is None or match.group(1) != table):
            raise RuntimeError("invalid final_test network identity")
        if type(uid) is not int or not 1000 <= uid <= 65535:
            raise RuntimeError("invalid final_test network UID")
        if (not isinstance(ports,list) or len(ports) != 2 or
                any(type(port) is not int or not 1 <= port <= 65535 for port in ports) or
                len(set(ports)) != 2):
            raise RuntimeError("final_test network transition requires two distinct allowed ports")
        return table,cgroup,uid,list(ports)

    @staticmethod
    def _dependency_network_batch(*, table, uid, ports, dns, public):
        allowed=", ".join(map(str,ports))
        rules=[
            f"add rule inet {table} output meta skuid {uid} ip daddr 127.0.0.0/8 tcp dport {{ {allowed} }} accept",
            f"add rule inet {table} output meta skuid {uid} ip6 daddr ::1 tcp dport {{ {allowed} }} accept",
        ]
        if public:
            rules.extend([
                f"add rule inet {table} output meta skuid {uid} ip daddr {dns} udp dport 53 accept",
                f"add rule inet {table} output meta skuid {uid} ip daddr {dns} tcp dport 53 accept",
                f"add rule inet {table} output meta skuid {uid} ip daddr {{ 10.0.0.0/8, 100.64.0.0/10, 127.0.0.0/8, 169.254.0.0/16, 172.16.0.0/12, 192.168.0.0/16 }} drop",
                f"add rule inet {table} output meta skuid {uid} ip6 daddr {{ ::1, fc00::/7, fe80::/10 }} drop",
            ])
        else:
            rules.append(f"add rule inet {table} output meta skuid {uid} drop")
        return ("\n".join([f"flush chain inet {table} output",*rules])+"\n").encode()

    def set_dependency_network(self, name, isolation, *, public: bool):
        """Atomically open or reseal public egress for final dependency setup."""
        if type(public) is not bool:
            raise TypeError("public must be a bool")
        table,cgroup,uid,ports=self._final_network_identity(isolation)
        expected_table="c4g"+name.rsplit("-",1)[-1] if isinstance(name,str) else ""
        if table != expected_table:
            raise RuntimeError("final_test network identity does not match the distro")
        observed_uid=int(self.exec(name,["id","-u"],user="c4gprobe").stdout)
        if observed_uid != uid:
            raise RuntimeError("final_test network UID changed")
        observed_cgroup=self.exec(name,["/usr/bin/stat","-c","%n:%F",cgroup],user="root").stdout.decode().strip()
        if observed_cgroup != f"{cgroup}:directory":
            raise RuntimeError("final_test base cgroup is unavailable")
        dns=None
        if public:
            dns=self.exec(name,["/bin/sh","-c","awk '/^nameserver /{print $2; exit}' /etc/resolv.conf"],
                          user="root").stdout.decode().strip()
            try: dns=str(ipaddress.IPv4Address(dns))
            except (ipaddress.AddressValueError,ValueError) as exc:
                raise RuntimeError("invalid WSL resolver address") from exc
        batch=self._dependency_network_batch(table=table,uid=uid,ports=ports,dns=dns,public=public)
        self.exec(name,["/usr/sbin/nft","-f","-"],user="root",input=batch)
        raw=self.exec(name,["/usr/sbin/nft","-j","list","table","inet",table],user="root").stdout.decode()
        role="development" if public else "final_test"
        _verify_nft_ruleset(raw,table=table,role=role,uid=uid,allowed_ports=ports,dns=dns)
        wrapper=('cg=$1; uid=$2; shift 2; echo $$ > "$cg/cgroup.procs"; '
                 'exec setpriv --reuid="$uid" --regid="$uid" --init-groups "$@"')
        if public:
            code=("import socket,ssl; h='pypi.org'; "
                  "socket.getaddrinfo(h,443,type=socket.SOCK_STREAM); "
                  "s=socket.create_connection((h,443),5); s.settimeout(5); "
                  "ssl.create_default_context().wrap_socket(s,server_hostname=h).close()")
            canary="public_dns_https_succeeded"
        else:
            code=("import signal,socket\n"
                  "def timeout(*_): raise TimeoutError('network denial canary timed out')\n"
                  "def denied(operation):\n"
                  " old=signal.signal(signal.SIGALRM,timeout); signal.alarm(3)\n"
                  " try: operation()\n"
                  " except OSError: pass\n"
                  " else: raise SystemExit(9)\n"
                  " finally: signal.alarm(0); signal.signal(signal.SIGALRM,old)\n"
                  "denied(lambda:socket.create_connection(('1.1.1.1',443),1))\n"
                  "denied(lambda:socket.getaddrinfo('pypi.org',443,type=socket.SOCK_STREAM))")
            canary="direct_ip_and_dns_blocked"
        self.exec(name,["/bin/sh","-c",wrapper,"dependency-network-canary",cgroup,str(uid),
                        "/usr/bin/python3","-I","-B","-c",code],user="root",timeout=15)
        return {"role":"final_test","public":public,"table":table,"uid":uid,
                "allowed_ports":ports,"dns":dns,"nftables_verified":True,
                "network_canary":canary}

    def verify_child(self, name, pid, uid, cgroup, mount_namespace=None):
        if type(pid) is not int or pid <= 1: raise RuntimeError("invalid WSL child pid")
        if not isinstance(cgroup,str) or not cgroup.startswith("/sys/fs/cgroup/c4g"):
            raise RuntimeError("invalid WSL child cgroup")
        expected="/"+cgroup.removeprefix("/sys/fs/cgroup/").strip("/")
        command=f"set -eu; ps -o uid=,pgid=,pid= -p {pid}; cat /proc/{pid}/cgroup; readlink /proc/{pid}/ns/mnt"
        lines=self.exec(name,["/bin/sh","-c",command],user="root").stdout.decode().splitlines()
        raw=lines[0].split() if lines else []
        if len(raw)!=3 or tuple(map(int,raw)) != (uid,pid,pid):
            raise RuntimeError("WSL child identity or process group verification failed")
        memberships=[line.split(":",2)[-1] for line in lines[1:] if line.count(":")>=2]
        if expected not in memberships:
            raise RuntimeError("WSL child escaped the assigned cgroup")
        child_mount=lines[-1] if lines else ""
        if mount_namespace is not None and child_mount != mount_namespace:
            raise RuntimeError("WSL child escaped the private mount namespace")
        return {"pid":pid,"uid":uid,"pgid":pid,"cgroup":expected,"mount_namespace":child_mount}

    def verify_private_mount(self,name,pid,host_mount_namespace,resolver_target,resolver_scope):
        if type(pid) is not int or pid <= 1: raise RuntimeError("invalid WSL runtime pid")
        script=(f"set -eu; ns=$(readlink /proc/{pid}/ns/mnt); printf '%s\\n' \"$ns\"; "
                f"for p in dev/dxg mnt/wslg tmp/.X11-unix; do test ! -e /proc/{pid}/root/$p; done; "
                f"for p in null zero random urandom; do test -c /proc/{pid}/root/dev/$p; done; "
                f"mountpoint -q /proc/{pid}/root/dev/pts; mountpoint -q /proc/{pid}/root/dev/shm; "
                f"test -s /proc/{pid}/root/etc/resolv.conf")
        namespace=self.exec(name,["/bin/sh","-c",script],user="root").stdout.decode().strip()
        if not namespace or namespace == host_mount_namespace:
            raise RuntimeError("WSL runtime did not enter a private mount namespace")
        resolver={"available":True,"target":resolver_target,"scope":resolver_scope,"mode":None}
        if resolver_scope=="private_recreated":
            mode=self.exec(name,["/usr/bin/stat","-c","0%a",f"/proc/{pid}/root{resolver_target}"],user="root").stdout.decode().strip()
            if mode!="0444": raise RuntimeError("private WSL resolver copy has unsafe mode")
            resolver["mode"]="0o444"
        return {"private_mount_namespace":True,"mount_namespace":namespace,
                "desktop_entrypoints":{"/dev/dxg":False,"/mnt/wslg":False,"/tmp/.X11-unix":False},
                "essential_devices":{"null":True,"pts":True,"random":True,"shm":True,"urandom":True,"zero":True},
                "resolver":resolver}

    def assign_child_cgroup(self,name,isolation,handle,pid):
        if not isinstance(handle,str) or len(handle)!=32 or any(c not in "0123456789abcdef" for c in handle):
            raise RuntimeError("invalid WSL child handle")
        child=f"{isolation['cgroup']}/child-{handle}"
        script=f"set -eu; mkdir {child}; echo {pid} > {child}/cgroup.procs"
        self.exec(name,["/bin/sh","-c",script],user="root")
        return child

    def kill_child_cgroup(self,name,cgroup):
        if re.fullmatch(r'/sys/fs/cgroup/c4g[0-9a-f]{12}/child-[0-9a-f]{32}',cgroup) is None:
            raise ValueError('unsafe child cgroup cleanup path')
        source=Path(__file__).with_name('cgroup_cleanup.py').read_bytes()
        self.exec(name,['/usr/bin/python3','-I','-B','-',cgroup,'kill'],
                  user='root',timeout=15,input=source)

    def teardown(self,name,isolation):
        table=isolation["table"]; cgroup=isolation["cgroup"]
        if not _RUN_TABLE.fullmatch(table) or cgroup!='/sys/fs/cgroup/'+table:
            raise ValueError('unsafe runtime cleanup identity')
        source=Path(__file__).with_name('cgroup_cleanup.py').read_bytes()
        try:
            self.exec(name,['/usr/bin/python3','-I','-B','-',cgroup,'remove'],
                      user='root',timeout=15,input=source)
        except subprocess.CalledProcessError as error:
            stderr=self._text((error.stderr or b'')[-4096:])
            number=re.search(r'\[Errno ([0-9]{1,4})\]',stderr)
            diagnostic=('populated_timeout' if 'TimeoutError: owned cgroup remains populated' in stderr
                        else f'errno={number[1]}' if number else 'unclassified')
            raise RuntimeError(
                f"WSL owned run cgroup cleanup failed (exit_code={error.returncode}; diagnostic={diagnostic})") from error
        tables=json.loads(self.exec(name,['/usr/sbin/nft','-j','list','tables'],user='root').stdout)
        if any(item.get('table',{}).get('family')=='inet' and
               item.get('table',{}).get('name')==table for item in tables['nftables']):
            self.exec(name,['/usr/sbin/nft','delete','table','inet',table],user='root')

    def terminate(self, name): self._run(["--terminate", name], timeout=30)
    def unregister(self, name): self._run(["--unregister", name], timeout=60)
