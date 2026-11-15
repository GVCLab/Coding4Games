import copy
import json

import pytest

from coding4game_gui.wsl2.native import WslNative, _verify_nft_ruleset


TABLE = "c4gabcdefabcdef"
UID = 23456
PORTS = [51001, 51002, 51005]
DNS = "10.255.255.254"


def match(left, right):
    return {"match": {"op": "==", "left": left, "right": right}}


def meta_uid(uid=UID):
    return match({"meta": {"key": "skuid"}}, uid)


def payload(protocol, field, right):
    return match({"payload": {"protocol": protocol, "field": field}}, right)


def prefix(addr, length):
    return {"prefix": {"addr": addr, "len": length}}


def rule(*expr):
    return {"rule": {"family": "inet", "table": TABLE, "chain": "output", "expr": list(expr), "handle": 1}}


def ruleset(role="development", allowed_ports=None):
    selected = allowed_ports if allowed_ports is not None else (PORTS if role == "development" else PORTS[:2])
    ports = {"set": selected}
    rules = [
        rule(meta_uid(), payload("ip", "daddr", prefix("127.0.0.0", 8)), payload("tcp", "dport", ports), {"accept": None}),
        rule(meta_uid(), payload("ip6", "daddr", "::1"), payload("tcp", "dport", ports), {"accept": None}),
    ]
    if role == "final_test":
        rules.append(rule(meta_uid(), {"drop": None}))
    else:
        rules.extend([
            rule(meta_uid(), payload("ip", "daddr", DNS), payload("udp", "dport", 53), {"accept": None}),
            rule(meta_uid(), payload("ip", "daddr", DNS), payload("tcp", "dport", 53), {"accept": None}),
            rule(meta_uid(), payload("ip", "daddr", {"set": [prefix("10.0.0.0", 8), prefix("100.64.0.0", 10),
                                                           prefix("127.0.0.0", 8), prefix("169.254.0.0", 16),
                                                           prefix("172.16.0.0", 12), prefix("192.168.0.0", 16)]}),
                 {"drop": None}),
            rule(meta_uid(), payload("ip6", "daddr", {"set": ["::1", prefix("fc00::", 7), prefix("fe80::", 10)]}),
                 {"drop": None}),
        ])
    return {"nftables": [
        {"metainfo": {"json_schema_version": 1}},
        {"table": {"family": "inet", "name": TABLE, "handle": 1}},
        {"chain": {"family": "inet", "table": TABLE, "name": "output", "handle": 1,
                   "type": "filter", "hook": "output", "prio": 0, "policy": "accept"}},
        *rules,
    ]}


@pytest.mark.parametrize("role", ["development", "final_test"])
def test_exact_generated_nft_policy_passes(role):
    ports = PORTS if role == "development" else PORTS[:2]
    _verify_nft_ruleset(json.dumps(ruleset(role)), table=TABLE, role=role, uid=UID,
                        allowed_ports=ports, dns=DNS if role == "development" else None)


def test_native_verify_uses_absolute_nft_path_and_accepts_observed_json(monkeypatch):
    native = WslNative()
    name = "C4G-WSL2-Run-f41f06710780-development-f7bc6bee9c94"
    table = "c4gf7bc6bee9c94"
    uid = 42084
    seen = []

    class Reply:
        def __init__(self, stdout): self.stdout = stdout

    def fake_exec(distro, argv, *, user="root", **kwargs):
        seen.append((argv, user))
        if argv == ["id", "-u"]: return Reply(b"0\n" if user == "root" else f"{uid}\n".encode())
        if argv[:2] == ["stat", "-c"]: return Reply(b"755\n")
        if argv[:2] == ["/bin/sh", "-c"]: return Reply((DNS + "\n").encode())
        if argv[0] == "/usr/sbin/nft":
            observed = json.dumps(ruleset()).replace(TABLE, table).replace(str(UID), str(uid))
            return Reply(observed.encode())
        if argv[0] == "/usr/bin/python3": return Reply(b'{"cv2":"4.10.0","numpy":"2.2.4"}\n')
        raise AssertionError(argv)

    monkeypatch.setattr(native, "exec", fake_exec)
    result = native.verify(name, role="development", control_port=51001, game_port=51002,
                           gateway_port=51005, python="/usr/bin/python3")
    assert result["uid"] == uid
    assert (["/usr/sbin/nft", "-j", "list", "table", "inet", table], "root") in seen


def invalid_mutations():
    wrong_uid = ruleset(); wrong_uid["nftables"][3]["rule"]["expr"][0]["match"]["right"] = UID + 1
    wrong_ports = ruleset(); wrong_ports["nftables"][3]["rule"]["expr"][2]["match"]["right"]["set"] = [51001, 51002]
    wrong_order = ruleset(); wrong_order["nftables"][3], wrong_order["nftables"][4] = wrong_order["nftables"][4], wrong_order["nftables"][3]
    wrong_policy = ruleset(); wrong_policy["nftables"][2]["chain"]["policy"] = "drop"
    extra_allow = ruleset(); extra_allow["nftables"].append(copy.deepcopy(extra_allow["nftables"][3]))
    wrong_family = ruleset(); wrong_family["nftables"][3]["rule"]["expr"][1]["match"]["left"]["payload"]["protocol"] = "ip6"
    return [wrong_uid, wrong_ports, wrong_order, wrong_policy, extra_allow, wrong_family]


@pytest.mark.parametrize("document", invalid_mutations())
def test_nft_policy_rejects_wrong_identity_ports_order_policy_extra_allow_and_family(document):
    with pytest.raises(RuntimeError):
        _verify_nft_ruleset(json.dumps(document), table=TABLE, role="development", uid=UID,
                            allowed_ports=PORTS, dns=DNS)


def final_isolation(**changes):
    value={"role":"final_test","table":TABLE,"cgroup":f"/sys/fs/cgroup/{TABLE}",
           "uid":UID,"allowed_ports":PORTS[:2]}
    value.update(changes)
    return value


@pytest.mark.parametrize(("public","role","canary"),[
    (True,"development","public_dns_https_succeeded"),
    (False,"final_test","direct_ip_and_dns_blocked"),
])
def test_dependency_network_atomically_replaces_verifies_and_canaries(monkeypatch,public,role,canary):
    native=WslNative(); calls=[]; policy={"role":"final_test"}
    name="C4G-WSL2-Run-profile-final_test-abcdefabcdef"

    class Reply:
        def __init__(self,stdout=b""): self.stdout=stdout

    def fake_exec(distro,argv,**kwargs):
        calls.append((argv,kwargs))
        if argv==["id","-u"]: return Reply(f"{UID}\n".encode())
        if argv[:3]==["/usr/bin/stat","-c","%n:%F"]:
            return Reply(f"/sys/fs/cgroup/{TABLE}:directory\n".encode())
        if argv[:2]==["/bin/sh","-c"] and "awk " in argv[2]: return Reply((DNS+"\n").encode())
        if argv==["/usr/sbin/nft","-f","-"]:
            batch=kwargs["input"].decode()
            assert batch.startswith(f"flush chain inet {TABLE} output\n")
            assert "delete table" not in batch and "add table" not in batch
            policy["role"]="development" if "udp dport 53 accept" in batch else "final_test"
            return Reply()
        if argv==["/usr/sbin/nft","-j","list","table","inet",TABLE]:
            return Reply(json.dumps(ruleset(policy["role"],PORTS[:2])).encode())
        if argv[:2]==["/bin/sh","-c"] and argv[3]=="dependency-network-canary": return Reply()
        raise AssertionError(argv)

    monkeypatch.setattr(native,"exec",fake_exec)
    result=native.set_dependency_network(name,final_isolation(),public=public)
    assert result["public"] is public and result["network_canary"]==canary
    mutation=next(i for i,call in enumerate(calls) if call[0]==["/usr/sbin/nft","-f","-"])
    verification=next(i for i,call in enumerate(calls) if call[0][:3]==["/usr/sbin/nft","-j","list"])
    probe=next(i for i,call in enumerate(calls) if len(call[0])>3 and call[0][3]=="dependency-network-canary")
    assert mutation < verification < probe
    assert calls[mutation][1]["user"]=="root" and calls[mutation][1]["input"]
    probe_argv=calls[probe][0]
    assert probe_argv[4:6]==[f"/sys/fs/cgroup/{TABLE}",str(UID)]
    compile(probe_argv[-1],"<dependency-network-canary>","exec")
    assert "pypi.org" in probe_argv[-1]
    if not public:
        assert "1.1.1.1" in probe_argv[-1]
        assert "socket.getaddrinfo" in probe_argv[-1]
        assert "signal.alarm(3)" in probe_argv[-1]


@pytest.mark.parametrize("change",[
    {"role":"development"},
    {"table":"c4gfeedfacefeed"},
    {"cgroup":"/sys/fs/cgroup/other"},
    {"uid":"23456"},
    {"allowed_ports":[51001,51002,51005]},
    {"allowed_ports":[51001,51001]},
])
def test_dependency_network_refuses_invalid_final_identity_before_native_calls(monkeypatch,change):
    native=WslNative()
    monkeypatch.setattr(native,"exec",lambda *a,**k:pytest.fail("must fail before native calls"))
    with pytest.raises(RuntimeError):
        native.set_dependency_network("C4G-WSL2-Run-profile-final_test-abcdefabcdef",
                                      final_isolation(**change),public=True)


def test_dependency_network_refuses_observed_uid_change_before_nft_mutation(monkeypatch):
    native=WslNative(); calls=[]
    def fake_exec(name,argv,**kwargs):
        calls.append(argv)
        return type("Reply",(),{"stdout":b"23457\n"})()
    monkeypatch.setattr(native,"exec",fake_exec)
    with pytest.raises(RuntimeError,match="UID changed"):
        native.set_dependency_network("C4G-WSL2-Run-profile-final_test-abcdefabcdef",
                                      final_isolation(),public=False)
    assert all(argv[:2] != ["/usr/sbin/nft","-f"] for argv in calls)


def test_dependency_network_fails_closed_when_observed_policy_is_tampered(monkeypatch):
    native=WslNative(); canary=[]
    name="C4G-WSL2-Run-profile-final_test-abcdefabcdef"
    tampered=ruleset("final_test")
    tampered["nftables"].append(copy.deepcopy(tampered["nftables"][3]))
    def fake_exec(distro,argv,**kwargs):
        if argv==["id","-u"]: stdout=f"{UID}\n".encode()
        elif argv[0]=="/usr/bin/stat": stdout=f"/sys/fs/cgroup/{TABLE}:directory\n".encode()
        elif argv==["/usr/sbin/nft","-f","-"]: stdout=b""
        elif argv[:3]==["/usr/sbin/nft","-j","list"]: stdout=json.dumps(tampered).encode()
        else: canary.append(argv); stdout=b""
        return type("Reply",(),{"stdout":stdout})()
    monkeypatch.setattr(native,"exec",fake_exec)
    with pytest.raises(RuntimeError,match="unexpected WSL nftables"):
        native.set_dependency_network(name,final_isolation(),public=False)
    assert canary==[]
