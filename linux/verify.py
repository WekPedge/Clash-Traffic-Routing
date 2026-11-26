"""End-to-end checks for the Codex-only NYC route. Run as the logged-in Codex user."""
import argparse
import http.client
import http.server
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import threading
import stat
import subprocess
import sys
import tempfile
import time

import clash_route

NY_ROUTE = clash_route.NAME
LEGACY_ROUTE = "Codex-NY-Ubuntu"
CURL = "/usr/bin/curl"
MODEL_HOSTS = {"chatgpt.com", "api.openai.com"}


class VerificationError(Exception):
    pass


def controller_path():
    override = os.environ.get("CODEX_NY_CONTROLLER")
    if override:
        if not os.path.isabs(override):
            raise VerificationError("CODEX_NY_CONTROLLER 须为 Unix socket 的绝对路径。")
        return override
    guesses = [f"/run/clash-verge-service/users/{os.getuid()}/verge-mihomo.sock",
               "/tmp/verge/verge-mihomo.sock", "/tmp/verge-mihomo.sock"]
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if "mihomo" not in (entry / "comm").read_text().lower():
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
            if b"-ext-ctl-unix" in args:
                guesses.insert(0, os.fsdecode(args[args.index(b"-ext-ctl-unix") + 1]))
        except (OSError, IndexError):
            continue
    for path in guesses:
        try:
            if os.path.isabs(path) and stat.S_ISSOCK(os.stat(path).st_mode):
                return path
        except OSError:
            pass
    raise VerificationError("未定位 Clash 控制 socket，请指定 CODEX_NY_CONTROLLER。")


class Controller(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def api(path, resource):
    client = Controller(path)
    try:
        client.request("GET", resource)
        response = client.getresponse()
        if response.status != 200:
            raise VerificationError(f"Clash {resource} 返回 HTTP {response.status}。")
        return json.load(response)
    finally:
        client.close()


def address(raw):
    host, port = raw.split(":")
    decoded = bytes.fromhex(host)
    packed = b"".join(int.from_bytes(decoded[i:i + 4], "big").to_bytes(4, sys.byteorder)
                      for i in range(0, len(decoded), 4))
    return normalize(str(ipaddress.ip_address(packed))), str(int(port, 16))


def normalize(ip):
    addr = ipaddress.ip_address(ip)
    return str(getattr(addr, "ipv4_mapped", None) or addr)


def active_sockets(pid):
    base = Path(f"/proc/{pid}")
    inodes = set()
    try:
        for fd in (base / "fd").iterdir():
            try:
                target = os.readlink(fd)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    except (FileNotFoundError, ProcessLookupError):
        return set()
    sockets = set()
    if inodes:
        for table in ("tcp", "tcp6"):
            try:
                lines = (base / "net" / table).read_text().splitlines()[1:]
            except FileNotFoundError:
                continue
            for row in lines:
                cols = row.split()
                if len(cols) >= 10 and cols[3] == "01" and cols[9] in inodes:
                    sockets.add((address(cols[1]), address(cols[2])))
    return sockets


def sample(controller, proc, executable, records):
    sockets = active_sockets(proc.pid)
    if not sockets:
        return
    for flow in api(controller, "/connections").get("connections") or []:
        m = flow.get("metadata", {})
        host = (m.get("host") or m.get("sniffHost") or "").lower().rstrip(".")
        if host not in MODEL_HOSTS or not m.get("sourceIP"):
            continue
        path = m.get("processPath") or ""
        if path and os.path.realpath(path) != executable:
            continue
        src = (normalize(m["sourceIP"]), str(m["sourcePort"]))
        # TUN's destination IP may be empty on a fake-IP connection. For local
        # proxy inbounds the peer port is the listener, not the target's port.
        tun = str(m.get("type", "")).lower() == "tun"
        matched = any(s == src and (not tun or peer[1] == str(m["destinationPort"]))
                      for s, peer in sockets)
        if matched:
            records[flow["id"]] = (host, tuple(flow.get("chains", [])),
                                   flow.get("upload", 0), flow.get("download", 0))


def run_observed(cmd, cwd, timeout, controller):
    records = {}
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=output,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    raise VerificationError(f"{Path(cmd[0]).name} 请求超时。")
                sample(controller, proc, os.path.realpath(cmd[0]), records)
                time.sleep(.04)
            output.seek(0)
            body = output.read(16384).decode(errors="replace").strip()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    return proc.returncode, body, list(records.values())


def trace_url():
    return "https://chatgpt.com/cdn-cgi/trace?codex_ny_check=" + secrets.token_hex(10)


def trace_ip(body):
    fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    if fields.get("h") != "chatgpt.com" or abs(float(fields.get("ts", 0)) - time.time()) > 180:
        raise VerificationError("chatgpt.com 未返回实时出口 IP。")
    return str(ipaddress.ip_address(fields.get("ip", "")))


def native_trace(engine, cwd):
    # The stock Codex Responses relay makes the TLS request. Its disposable HOME
    # and placeholder input keep account credentials out of this separate probe.
    url = trace_url()

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Host") != "chatgpt.com" or self.path != "http://chatgpt.com/codex-ny-local-test":
                self.send_error(400)
                return
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(303)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    with http.server.HTTPServer(("127.0.0.1", 0), Redirect) as redirect:
        server = threading.Thread(target=redirect.serve_forever, daemon=True)
        server.start()
        info = Path(cwd) / "probe-port"
        env = {k: v for k, v in os.environ.items()
               if k.lower() in ("https_proxy", "all_proxy", "no_proxy") or k in ("SSL_CERT_FILE", "SSL_CERT_DIR")}
        env.update(HOME=cwd, CODEX_HOME=cwd, PATH="/usr/bin:/bin",
                   http_proxy="http://127.0.0.1:" + str(redirect.server_port))
        cmd = [engine, "responses-api-proxy", "--port", "0", "--server-info", str(info),
               "--upstream-url", "http://chatgpt.com/codex-ny-local-test"]
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            p.stdin.write(b"local-network-test-placeholder\n")
            p.stdin.close()
            deadline = time.monotonic() + 5
            while not info.exists() and p.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            if not info.exists():
                raise VerificationError("本机 Codex 未提供可用的 responses-api-proxy IP 回显。")
            conn = http.client.HTTPConnection("127.0.0.1", json.loads(info.read_text())["port"], timeout=20)
            try:
                conn.request("POST", "/v1/responses", b"{}", {"Content-Type": "application/json",
                             "User-Agent": "curl/8.7.1", "Cache-Control": "no-cache"})
                resp = conn.getresponse()
                if resp.status != 200:
                    raise VerificationError(f"Codex 的 IP 回显收到 HTTP {resp.status}。")
                return trace_ip(resp.read(8192).decode())
            finally:
                conn.close()
        finally:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except subprocess.TimeoutExpired:
                    p.kill()
            p.wait()
            redirect.shutdown()
            server.join(2)


def validate(result, engine, expected):
    ctl = controller_path()
    available = api(ctl, "/proxies")["proxies"]
    if expected == "baseline":
        expected = "on" if NY_ROUTE in available else "legacy" if LEGACY_ROUTE in available else "off"
    result.update(engine=engine, expected=expected, observed_mode=api(ctl, "/configs")["mode"])
    expected_ip = clash_route.node_settings()[1] if expected != "off" else None
    if (NY_ROUTE in available) != (expected == "on"):
        raise VerificationError("Clash 当前纽约节点状态与要求不一致。")

    with tempfile.TemporaryDirectory(prefix="codex-ny-check-") as cwd:
        ordinary_cmd = [CURL, "-qfsS", "--max-time", "20", "-H", "Cache-Control: no-cache", trace_url()]
        result["ordinary_chains"] = []
        try:
            rc, body, flows = run_observed(ordinary_cmd, cwd, 23, ctl)
            if rc:
                raise VerificationError(f"普通应用请求出口回显失败（curl {rc}）。")
            result["ordinary_chatgpt_ip"] = trace_ip(body)
            result["ordinary_chains"] = sorted({chain for _, chain, _, _ in flows})
        except (OSError, ValueError, http.client.HTTPException, VerificationError) as error:
            result["ordinary_trace_error"] = str(error)
        if any(NY_ROUTE in chain for chain in result["ordinary_chains"]):
            raise VerificationError("普通应用被错误地导向了纽约节点。")

        try:
            result["codex_chatgpt_ip"] = native_trace(engine, cwd)
            result["ip_evidence"] = "codex_binary_https_echo"
        except (OSError, ValueError, http.client.HTTPException, VerificationError) as e:
            # Continue the real request; an older CLI can lack the optional diagnostic.
            result["native_trace_error"] = str(e)
        login = subprocess.run([engine, "login", "status"], capture_output=True,
                               text=True, timeout=10, cwd=cwd)
        status_text = (login.stdout + login.stderr).lower()
        if login.returncode == 0:
            result["login"] = "chatgpt" if "chatgpt" in status_text else "api_key" if "api key" in status_text else "signed_in"
        else:
            result["login"] = "unconfirmed"
        marker = "NY_OK_" + secrets.token_hex(5)
        help_text = subprocess.run([engine, "exec", "--help"], capture_output=True, text=True, timeout=8).stdout
        args = [engine, "exec", "-c", 'model_provider="openai"', "--skip-git-repo-check", "--sandbox", "read-only"]
        if "--ephemeral" in help_text:
            args.append("--ephemeral")
        args.append("Do not use any tools. Reply with exactly " + marker)
        rc, body, flows = run_observed(args, cwd, 100, ctl)
        result["model_reply_verified"] = rc == 0 and body == marker
        result["model_flow_count"] = len(flows)
        result["model_chains"] = sorted({chain for _, chain, _, _ in flows})
        result["model_hosts"] = sorted({host for host, _, _, _ in flows})
        if not result["model_reply_verified"]:
            raise VerificationError(f"Codex 真实模型未返回确认文本（退出码 {rc}）。")
        if not flows:
            raise VerificationError("已收到模型回复，但未捕获到能归属于该 Codex 进程的模型连接。")
        if any(not c or ((NY_ROUTE in c) != (expected == "on"))
               or ((LEGACY_ROUTE in c) != (expected == "legacy")) for c in result["model_chains"]):
            raise VerificationError("Codex 模型请求的实际出站与开关状态不符。")
        if not result.get("ordinary_chatgpt_ip"):
            raise VerificationError(result["ordinary_trace_error"])
        route_ip = result.get("codex_chatgpt_ip")
        if not route_ip and expected in ("on", "legacy"):
            # Some backwards-compatible CLIs lack responses-api-proxy. This
            # fallback tests the same verified node, not a direct Codex echo.
            pinned = subprocess.run([CURL, "-qfsS", "--noproxy", "", "--max-time", "20",
                                     "--proxy", "socks5h://127.0.0.1:18739", trace_url()],
                                    text=True, capture_output=True, timeout=23)
            if pinned.returncode == 0:
                route_ip = trace_ip(pinned.stdout)
                result["matched_route_chatgpt_ip"] = route_ip
                result["ip_evidence"] = "same_node_https_echo"
        if not route_ip and expected == "off" and result["ordinary_chains"] == result["model_chains"]:
            route_ip = result["ordinary_chatgpt_ip"]
            result["matched_route_chatgpt_ip"] = route_ip
            result["ip_evidence"] = "same_chain_control_https_echo"
        if not route_ip:
            raise VerificationError("模型线路已验证，但未取得 Codex 或相同线路的同域出口回显。")
        if expected in ("on", "legacy") and route_ip != expected_ip:
            raise VerificationError("纽约出口 IP 不符。")
        if expected == "on" and result["ordinary_chatgpt_ip"] == expected_ip:
            raise VerificationError("普通应用和 Codex 的出口缺少区分。")


def verify(expected="baseline", as_json=False):
    result = {}
    try:
        engine = clash_route.engines()[0]
        validate(result, os.path.realpath(engine), expected)
        result["ok"] = True
    except (OSError, ValueError, RuntimeError, VerificationError, http.client.HTTPException, subprocess.SubprocessError) as error:
        result.update(ok=False, error=str(error))
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        if result.get("codex_chatgpt_ip"):
            print("Codex 访问 chatgpt.com 的出口", result["codex_chatgpt_ip"])
        else:
            print("相同线路访问 chatgpt.com 的对照出口", result.get("matched_route_chatgpt_ip") or "未验证")
        print("普通应用访问同一主机的出口", result.get("ordinary_chatgpt_ip") or "未验证")
        for chain in result.get("model_chains", []):
            print("Codex 真实连接", " → ".join(chain))
        print("真实模型请求和出口验证通过。" if result["ok"] else "验收失败：" + result["error"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect", choices=("baseline", "on", "off"), default="baseline")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    sys.exit(verify(args.expect, args.json))
