"""Install and remove the menu app's temporary process route in the local Mihomo core."""
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import quote

STATE = Path.home() / "Library/Application Support/Codex NY Route"
JOURNAL = STATE / "clash-restore.json"
PROFILE = Path.home() / "Library/Application Support/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml"
PREFERENCES = PROFILE.with_name("config.yaml")
NODE_PROFILE = Path.home() / ".config/codex-ny-hy2/ny-hy2-profile.yaml"
CONTROLLER = f"/var/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock"
NAME = "CodexNY-Hysteria2"
LISTENER = "CodexNY-Hysteria2-Probe"
ENGINE = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"

# General settings that Clash Verge can change at runtime independently of the generated file.
GENERAL = ("port", "socks-port", "redir-port", "tproxy-port", "mixed-port", "allow-lan",
           "bind-address", "mode", "log-level", "ipv6", "interface-name", "routing-mark",
           "tcp-concurrent", "find-process-mode", "unified-delay")


class Controller(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(CONTROLLER)


def api(method, path, body=None, timeout=20):
    c = Controller("localhost", timeout=timeout)
    try:
        c.request(method, path, json.dumps(body).encode() if body is not None else None,
                  {"Content-Type": "application/json"})
        resp = c.getresponse()
        data = resp.read()
        if resp.status == 404 and (path == "/proxies/" + NAME or method == "DELETE"):
            return None
        if resp.status >= 400:
            # Config-parse errors can echo certificates, passwords or subscription URLs.
            raise RuntimeError(f"Clash {method} {path.split('?')[0]} 返回 HTTP {resp.status}。")
        return json.loads(data) if data else None
    finally:
        c.close()


def live():
    return api("GET", "/proxies/" + NAME) is not None


def selected(proxies):
    return {name: p["now"] for name, p in proxies.items()
            if p.get("type") == "Selector" and p.get("now")}


def restore_selections(wanted):
    groups = api("GET", "/proxies")["proxies"]
    for name, target in wanted.items():
        group = groups.get(name, {})
        if target in group.get("all", []) and group.get("now") != target:
            api("PUT", "/proxies/" + quote(name, safe=""), {"name": target})


def save(journal):
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = JOURNAL.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(journal, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(JOURNAL)


def prepend_items(raw, section, items):
    head = rf"(?m)^{re.escape(section)}:[ \t]*$"
    entries = "".join("\n- " + json.dumps(item, ensure_ascii=False) for item in items)
    if re.search(head, raw):
        return re.sub(head, lambda m: m[0] + entries, raw, count=1)
    if re.search(rf"(?m)^{re.escape(section)}:", raw):
        raise RuntimeError(f"Clash 配置中的 {section} 不是预期的列表格式。")
    return raw.rstrip() + "\n" + section + ":" + entries + "\n"


def read_node_profile():
    if not NODE_PROFILE.is_file():
        raise RuntimeError(f"请先安装节点配置到 {NODE_PROFILE}。")
    stat = NODE_PROFILE.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise RuntimeError(f"请将 {NODE_PROFILE} 设为当前用户所有、权限 0600。")
    data = json.loads(NODE_PROFILE.read_text())
    matches = [p for p in data["proxies"] if p.get("type") == "hysteria2"]
    if len(matches) != 1:
        raise RuntimeError("节点文件应包含一个 Hysteria 2 节点。")
    proxy = dict(matches[0], name=NAME)
    if not proxy.get("password") or not proxy.get("sni") or not proxy.get("fingerprint") or proxy.get("skip-cert-verify"):
        raise RuntimeError("请核对节点密码、SNI 和证书指纹，并启用证书验证。")
    try:
        server = ipaddress.IPv4Address(proxy["server"])
        expected = ipaddress.ip_address(data.get("expected-egress-ip", str(server)))
    except (ValueError, KeyError):
        raise RuntimeError("请使用 VPS 的 IPv4 地址，并填写有效的公网出口 IP。") from None
    return proxy, str(expected)


def node():
    proxy, _ = read_node_profile()
    interface = proxy.get("interface-name")
    if not interface:
        state = subprocess.run(["/usr/sbin/scutil"], input="show State:/Network/Global/IPv4\n",
                               text=True, capture_output=True, check=True)
        match = re.search(r"(?m)^\s*PrimaryInterface\s*:\s*(\S+)", state.stdout)
        interface = match.group(1) if match else ""
    if (interface.startswith(("utun", "tun", "lo")) or
            interface not in {name for _, name in socket.if_nameindex()}):
        raise RuntimeError("请在节点的 interface-name 中指定连接 VPS 的物理网卡。")
    proxy["interface-name"] = interface
    return proxy


def render(raw, proxy, general, port):
    if NAME in raw:
        raise RuntimeError("当前 Clash 配置已使用纽约临时节点名。")
    mode = general["mode"].lower()
    rules = ["PROCESS-PATH," + ENGINE + "," + NAME]
    if mode in ("global", "direct"):
        rules.append("MATCH," + mode.upper())
    elif mode != "rule":
        raise RuntimeError("无法识别当前 Clash 模式。")
    raw = prepend_items(raw, "proxies", [proxy])
    raw = prepend_items(raw, "rules", rules)
    raw = prepend_items(raw, "listeners", [{"name": LISTENER, "type": "mixed", "listen": "127.0.0.1",
                                            "port": port, "proxy": NAME}])
    params = dict(general, mode="rule", **{"find-process-mode": "always"})
    for key, value in params.items():
        pattern = rf"(?m)^{re.escape(key)}:[^\n]*$"
        line = key + ": " + json.dumps(value, ensure_ascii=False)
        if re.search(pattern, raw):
            raw = re.sub(pattern, lambda _m: line, raw, count=1)
        else:
            raw += "\n" + line + "\n"
    return raw


def identities(rules):
    return [(r["type"], r["payload"], r["proxy"]) for r in rules]


def preference_mode():
    # Clash Verge persists its mode here even if Mihomo is already in rule mode.
    try:
        text = PREFERENCES.read_text()
    except OSError:
        return None
    match = re.search(r"""(?im)^mode:[ \t]*["']?(rule|global|direct)["']?[ \t]*(?:\#.*)?$""", text)
    return match.group(1).lower() if match else None


def requested_mode(journal, runtime):
    pref = preference_mode()
    previous = journal.get("preference_mode")
    if pref is not None:
        journal["preference_mode"] = pref
        if previous is not None and pref != previous:
            return pref
    # Our overlay enforces rule mode; any live global/direct is a new selection.
    return runtime if runtime in ("global", "direct") else journal["general"]["mode"]


def prefix(mode):
    wanted = [("ProcessPath", ENGINE, NAME)]
    if mode in ("global", "direct"):
        wanted.append(("Match", "", mode.upper()))
    return wanted


def owns_rules(rules, mode):
    wanted = prefix(mode)
    top = rules[:len(wanted)]
    return (identities(top) == wanted and not any(r.get("extra", {}).get("disabled") for r in top))


def healthy(port):
    if not JOURNAL.exists():
        return False
    journal = json.loads(JOURNAL.read_text())
    if port != journal["port"]:
        return False
    settings = api("GET", "/configs", timeout=5)
    rules = api("GET", "/rules", timeout=5)["rules"]
    return (settings["mode"] == "rule" and settings["tun"]["enable"] and settings["find-process-mode"] != "off"
            and owns_rules(rules, journal["general"]["mode"]) and live())


def apply_overlay(journal, base_rules, proxies):
    mode = journal["general"]["mode"]
    content = render(PROFILE.read_text(), node(), journal["general"], journal["port"])
    journal["phase"] = "prepared"
    journal["selected"] = selected(proxies)
    save(journal)
    api("PUT", "/configs?force=false", {"payload": content}, timeout=45)
    # Verify the generated subscription against the live core, both on startup and on mode changes.
    post_proxies = api("GET", "/proxies")["proxies"]
    post_rules = api("GET", "/rules")["rules"]
    if (set(post_proxies) != set(proxies) | {NAME} or identities(post_rules[len(prefix(mode)):]) != base_rules
            or not healthy(journal["port"])):
        raise RuntimeError("Clash 的运行配置与当前订阅不一致，已请求恢复。")
    restore_selections(journal["selected"])
    journal["phase"] = "active"
    save(journal)


def install(port):
    if JOURNAL.exists() or live():
        raise RuntimeError("此前的纽约线路仍待恢复。")
    pref = preference_mode()
    settings = api("GET", "/configs")
    if not settings.get("tun", {}).get("enable"):
        raise RuntimeError("请先启用 Clash Verge 的 TUN。")
    if not os.path.isfile(ENGINE):
        raise RuntimeError("找不到该 Mac 上的 Codex 引擎。")
    general = {key: settings[key] for key in GENERAL if key in settings}
    journal = {"general": general, "preference_mode": pref, "port": port}
    apply_overlay(journal, identities(api("GET", "/rules")["rules"]), api("GET", "/proxies")["proxies"])


def sync_mode(port):
    if not JOURNAL.exists():
        return False
    journal = json.loads(JOURNAL.read_text())
    if journal["port"] != port or journal["phase"] != "active":
        return False
    settings = api("GET", "/configs", timeout=5)
    rules = api("GET", "/rules", timeout=5)["rules"]
    old = journal["general"]["mode"]
    if (not settings["tun"]["enable"] or settings["find-process-mode"] == "off"
            or not owns_rules(rules, old) or not live()):
        return False
    previous_pref = journal.get("preference_mode")
    mode = requested_mode(journal, settings["mode"])
    if mode != old:
        journal["general"]["mode"] = mode
        proxies = api("GET", "/proxies")["proxies"]
        if NAME not in proxies:
            return False
        apply_overlay(journal, identities(rules[len(prefix(old)):]), proxies)
    else:
        if journal.get("preference_mode") != previous_pref:
            save(journal)
        if settings["mode"] != "rule":
            api("PATCH", "/configs", {"mode": "rule"})
    return True


def restore():
    if not JOURNAL.exists():
        if live():
            raise RuntimeError("找到了同名代理，但缺少本工具的恢复记录。")
        return
    journal = json.loads(JOURNAL.read_text())
    if live():
        # Honor selector changes made in Verge while the temporary rules were active.
        if journal["phase"] == "active":
            journal["general"]["mode"] = requested_mode(journal, api("GET", "/configs")["mode"])
            journal["selected"] = selected(api("GET", "/proxies")["proxies"])
        journal["phase"] = "restoring"
        save(journal)
        api("PUT", "/configs?force=false", {"path": ""}, timeout=45)
    elif journal["phase"] != "restoring":
        # The caller never installed the profile, or Verge superseded it with a new one.
        JOURNAL.unlink()
        return
    current = api("GET", "/configs")
    changes = {k: v for k, v in journal["general"].items() if current.get(k) != v}
    if changes:
        api("PATCH", "/configs", changes)
    restore_selections(journal["selected"])
    if live() or api("GET", "/configs")["mode"] != journal["general"]["mode"]:
        raise RuntimeError("Clash 尚未恢复之前的线路。")
    JOURNAL.unlink()
