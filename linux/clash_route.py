"""Transaction for a Codex-only Hysteria 2 route in the user's running Mihomo."""
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from urllib.parse import quote

import yaml

CFG = Path.home() / ".config/codex-ny"
JOURNAL = CFG / "clash-restore.json"
PROFILE = Path.home() / ".local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
NODE_PROFILE = CFG / "ny-hy2-profile.yaml"
ENGINE = "/usr/lib/chatgpt/resources/codex"
NAME = "CodexNY-Hysteria2"
LISTENER = NAME + "-Probe"
PORT = 18739
GENERAL = ("port", "socks-port", "redir-port", "tproxy-port", "mixed-port", "allow-lan",
           "bind-address", "mode", "log-level", "ipv6", "interface-name", "routing-mark",
           "tcp-concurrent", "find-process-mode", "unified-delay")


class Controller(http.client.HTTPConnection):
    def connect(self):
        address = os.environ.get("CODEX_NY_CONTROLLER") or f"/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock"
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(address)


def api(method, path, body=None, timeout=12):
    client = Controller("localhost", timeout=timeout)
    try:
        client.request(method, path, json.dumps(body).encode() if body is not None else None,
                       {"Content-Type": "application/json"})
        response = client.getresponse()
        data = response.read()
        if response.status == 404 and (method == "DELETE" or path == "/proxies/" + NAME):
            return None
        if response.status >= 400:
            # Parse errors may contain a node credential or subscription URL.
            raise RuntimeError(f"Clash {method} {path.split('?')[0]} 返回 HTTP {response.status}。")
        return json.loads(data) if data else None
    finally:
        client.close()


def live():
    return api("GET", "/proxies/" + NAME) is not None


def selected():
    return {name: p["now"] for name, p in api("GET", "/proxies")["proxies"].items()
            if p.get("type") == "Selector" and p.get("now")}


def restore_selections(wanted):
    proxies = api("GET", "/proxies")["proxies"]
    for name, target in wanted.items():
        group = proxies.get(name, {})
        if target != group.get("now") and target in group.get("all", []):
            api("PUT", "/proxies/" + quote(name, safe=""), {"name": target})


def save(journal):
    with tempfile.NamedTemporaryFile(dir=CFG, mode="w", delete=False) as f:
        json.dump(journal, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, JOURNAL)


def public_ipv4(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        ip = ipaddress.IPv4Address(value)
        if not ip.is_global or ip.is_multicast:
            raise ValueError
    except ValueError:
        raise RuntimeError("请在节点配置中填写有效的公网 IPv4 地址。") from None
    return str(ip)


def node_settings():
    stat = NODE_PROFILE.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"请将 {NODE_PROFILE} 设为当前用户所有、权限 0600。")
    try:
        config = yaml.safe_load(NODE_PROFILE.read_text())
    except yaml.YAMLError:
        raise RuntimeError("无法解析 Hysteria 2 节点文件。") from None
    nodes = config.get("proxies") if isinstance(config, dict) else None
    if (not isinstance(nodes, list) or len(nodes) != 1 or not isinstance(nodes[0], dict)
            or nodes[0].get("type") != "hysteria2"):
        raise RuntimeError("节点文件需要包含一个 Hysteria 2 代理。")
    proxy = dict(nodes[0], name=NAME)
    fingerprint = proxy.get("fingerprint")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}|[0-9a-fA-F]{64}", fingerprint):
        raise RuntimeError("节点需要 SHA-256 证书指纹。")
    if proxy.get("skip-cert-verify", False) is not False:
        raise RuntimeError("节点需要启用 TLS 证书验证。")
    if not all(isinstance(proxy.get(k), str) and proxy[k].strip() for k in ("password", "sni")):
        raise RuntimeError("节点缺少密码或 TLS SNI。")
    if type(proxy.get("port")) is not int or not 1 <= proxy["port"] <= 65535:
        raise RuntimeError("节点端口须为 1 至 65535 之间的整数。")
    server = public_ipv4(proxy.get("server"))
    expected_ip = public_ipv4(config["expected-egress-ip"]) if "expected-egress-ip" in config else server
    return proxy, expected_ip


def node():
    proxy, expected_ip = node_settings()
    interface = proxy.get("interface-name")
    if not interface or interface == "auto":
        rows = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "table", "main", "default"]))
        candidates = [r for r in rows if r.get("dev") and r.get("type", "unicast") == "unicast"
                      and r["dev"] != "lo" and "linkdown" not in r.get("flags", [])]
        if not candidates:
            raise RuntimeError("未找到 Linux IPv4 默认出口网卡。")
        interface = min(candidates, key=lambda r: r.get("metric", 0))["dev"]
    if not any(name == interface for _, name in socket.if_nameindex()):
        raise RuntimeError(f"找不到纽约节点指定的网卡 {interface}。")
    proxy["interface-name"] = interface
    return proxy, expected_ip


def node_revision(node_config):
    # The journal stores a digest rather than the node password.
    return hashlib.sha256(json.dumps(node_config, sort_keys=True).encode()).hexdigest()


def current_node_matches():
    if not JOURNAL.exists():
        return False
    journal = json.loads(JOURNAL.read_text())
    return journal["phase"] == "active" and journal.get("node_revision") == node_revision(node())


def engines():
    # The original install provides ~/.local/bin/codex as a link to the desktop engine.
    # An additional native CLI may be configured as an absolute path on this user's machine.
    paths = [ENGINE, str(Path.home() / ".local/bin/codex")]
    paths_file = CFG / "engine-paths"
    if paths_file.exists():
        paths.extend(line.strip() for line in paths_file.read_text().splitlines() if line.strip())
    result = []
    for p in paths:
        path = Path(p).resolve()
        if path in result or not path.is_file() or not os.access(path, os.X_OK):
            continue
        with path.open("rb") as f:
            if f.read(4) == b"\x7fELF":
                result.append(path)
    if not result:
        raise RuntimeError("未找到原生 Codex 引擎。")
    return [str(p) for p in result]


def identities(rules):
    return [(r["type"], r["payload"], r["proxy"]) for r in rules]


def wanted_rules(paths, mode):
    rules = [("ProcessPath", p, NAME) for p in paths]
    if mode in ("global", "direct"):
        rules.append(("Match", "", mode.upper()))
    elif mode != "rule":
        raise RuntimeError("无法识别当前 Clash 模式。")
    return rules


def healthy(journal):
    cfg = api("GET", "/configs")
    rules = api("GET", "/rules")["rules"]
    wanted = wanted_rules(journal["engines"], journal["general"]["mode"])
    return (cfg["mode"] == "rule" and cfg["tun"]["enable"] and cfg.get("find-process-mode") != "off"
            and identities(rules[:len(wanted)]) == wanted and live()
            and not any(r.get("extra", {}).get("disabled") for r in rules[:len(wanted)]))


def flows():
    return api("GET", "/connections").get("connections") or []


def old_flows(paths):
    # In global mode the core sometimes omits processPath. Cross-check exact owned sockets in ss.
    owned = set()
    output = subprocess.check_output(["ss", "-Hntp", "state", "established"], text=True)
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        source_ip, _, source_port = fields[2].rpartition(":")
        _, _, peer_port = fields[3].rpartition(":")
        for pid in re.findall(r"pid=(\d+)", line):
            try:
                binary = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                continue
            if binary in paths:
                owned.add((source_ip.strip("[]").removeprefix("::ffff:"), source_port, peer_port))
    result = set()
    for flow in flows():
        m = flow["metadata"]
        if NAME in flow.get("chains", []):
            continue
        src = m["sourceIP"].removeprefix("::ffff:"), str(m["sourcePort"])
        ports = (m.get("destinationPort"), m.get("inboundPort"))
        if m.get("processPath") in paths or (not m.get("processPath") and
                any((*src, str(p)) in owned for p in ports)):
            result.add(flow["id"])
    return result


def close_flows(ids):
    for cid in ids:
        api("DELETE", "/connections/" + quote(cid, safe=""))


def exit_ip():
    for url in ("https://chatgpt.com/cdn-cgi/trace", "https://api.ipify.org"):
        result = subprocess.run(["curl", "-qfsS", "--noproxy", "", "--proxy", f"http://127.0.0.1:{PORT}",
                                 "--connect-timeout", "4", "--max-time", "9", "-H", "Cache-Control: no-cache",
                                 url + "?t=" + str(time.time_ns())], capture_output=True, text=True)
        if result.returncode == 0:
            val = next((s[3:] for s in result.stdout.splitlines() if s.startswith("ip=")), result.stdout.strip())
            try:
                return str(ipaddress.ip_address(val))
            except ValueError:
                continue
    return None


def install():
    if JOURNAL.exists() or live():
        raise RuntimeError("上次纽约分流仍待恢复。")
    settings = api("GET", "/configs")
    if not settings.get("tun", {}).get("enable"):
        raise RuntimeError("请在 Clash Verge 中开启 TUN。")
    # An independently occupied probe port could report a misleading IP after the reload.
    with socket.socket() as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        check.bind(("127.0.0.1", PORT))
    paths = engines()
    general = {k: settings[k] for k in GENERAL if k in settings}
    baseline = identities(api("GET", "/rules")["rules"])
    orig_proxies = set(api("GET", "/proxies")["proxies"])
    try:
        cfg = yaml.load(PROFILE.read_bytes(), Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
    except yaml.YAMLError:
        raise RuntimeError("无法解析 Clash Verge 当前生成的订阅配置。") from None
    if any(p["name"] == NAME for p in cfg.get("proxies", [])) or any(p["name"] == LISTENER for p in cfg.get("listeners", [])):
        raise RuntimeError("当前订阅已占用纽约测试节点名。")
    new_rules = [f"PROCESS-PATH,{p},{NAME}" for p in paths]
    if general["mode"] in ("global", "direct"):
        new_rules.append("MATCH," + general["mode"].upper())
    wanted_rules(paths, general["mode"])
    cfg.update(general)
    cfg.update({"mode": "rule", "find-process-mode": "always"})
    cfg.setdefault("tun", {})["enable"] = True
    proxy, expected_ip = node()
    cfg["proxies"] = [proxy] + (cfg.get("proxies") or [])
    cfg["rules"] = new_rules + (cfg.get("rules") or [])
    cfg["listeners"] = [{"name": LISTENER, "type": "mixed", "listen": "127.0.0.1", "port": PORT, "proxy": NAME}] + (cfg.get("listeners") or [])
    stale = old_flows(paths)
    journal = {"phase": "prepared", "general": general, "selected": selected(), "engines": paths,
               "expected_ip": expected_ip, "node_revision": node_revision((proxy, expected_ip))}
    save(journal)
    api("PUT", "/configs?force=false", {"payload": json.dumps(cfg, ensure_ascii=False)}, timeout=45)
    if (not healthy(journal) or set(api("GET", "/proxies")["proxies"]) != orig_proxies | {NAME}
            or identities(api("GET", "/rules")["rules"][len(new_rules):]) != baseline):
        raise RuntimeError("Clash 当前订阅与运行状态不一致。")
    restore_selections(journal["selected"])
    if exit_ip() != expected_ip:
        raise RuntimeError("未确认 Hysteria 2 的纽约公网出口。")
    close_flows(stale | old_flows(paths))
    journal["phase"] = "active"
    save(journal)
    return journal


def restore():
    if not JOURNAL.exists():
        return
    journal = json.loads(JOURNAL.read_text())
    if live():
        used = [f["id"] for f in flows() if NAME in f.get("chains", [])]
        if journal["phase"] == "active":
            journal["selected"] = selected()
        journal["phase"] = "restoring"
        journal["flows"] = used
        save(journal)
        api("PUT", "/configs?force=false", {"path": ""}, timeout=45)
    elif journal["phase"] != "restoring":
        # Clash may have loaded a different subscription while the service was running.
        JOURNAL.unlink()
        return
    cfg = api("GET", "/configs")
    changes = {k: v for k, v in journal["general"].items() if cfg.get(k) != v}
    if changes:
        api("PATCH", "/configs", changes)
    restore_selections(journal["selected"])
    close_flows(journal.get("flows", []))
    if live() or api("GET", "/configs")["mode"] != journal["general"]["mode"]:
        raise RuntimeError("Clash 尚未恢复原线路。")
    JOURNAL.unlink()
