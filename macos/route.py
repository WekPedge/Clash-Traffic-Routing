"""Foreground worker for the Hysteria 2 menu switch."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from urllib.parse import quote

# The signed bundle launches Python in isolated mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clash_route as clash

STATE = Path.home() / "Library/Application Support/Codex NY Route"
stopping = False


def write_status(phase, message, port=None):
    temp = STATE / "status.tmp"
    fields = {"phase": phase, "message": message, "pid": str(os.getpid())}
    if port is not None:
        fields["port"] = str(port)
    temp.write_text(json.dumps(fields, ensure_ascii=False))
    temp.replace(STATE / "status.json")


def measure_vps_latency():
    # Use the same physical interface as the Hysteria 2 node. The echo terminates at the VPS.
    node = clash.node()
    interface = node["interface-name"]
    probe = subprocess.run(["/sbin/ping", "-n", "-b", interface, "-c", "5", "-W", "2000",
                            "-t", "9", node["server"]], capture_output=True, text=True, timeout=12,
                           env=dict(os.environ, LC_ALL="C"))
    samples = [float(n) for n in re.findall(r"time=([0-9.]+) ms", probe.stdout)]
    if not samples:
        raise RuntimeError("纽约 VPS 未回复 ICMP 延迟探测。")
    return interface, samples

def exit_ip(port):
    for url in ("https://chatgpt.com/cdn-cgi/trace", "https://api.ipify.org"):
        result = subprocess.run(["/usr/bin/curl", "-qfsS", "--noproxy", "",
                                 "--proxy", f"http://127.0.0.1:{port}", "--connect-timeout", "4",
                                 "--max-time", "8", "-H", "Cache-Control: no-cache",
                                 url + "?t=" + str(time.time_ns())], capture_output=True, text=True)
        if result.returncode == 0:
            text = next((s[3:] for s in result.stdout.splitlines() if s.startswith("ip=")),
                        result.stdout.strip())
            try:
                return str(ipaddress.ip_address(text))
            except ValueError:
                continue
    return None


def test_route(port):
    if not clash.healthy(port):
        return False, "Clash 中的 Codex 进程分流未生效。"
    _, expected = clash.read_node_profile()
    ip = exit_ip(port)
    if ip == expected:
        return True, f"Codex 进程规则已生效。Hysteria 2 实测纽约出口 {ip}。"
    if ip:
        return False, f"预期纽约出口 {expected}，实测为 {ip}。"
    return False, "无法通过 Hysteria 2 验证纽约出口。"


def connections():
    return clash.api("GET", "/connections", timeout=5).get("connections") or []


def codex_tcp_endpoints():
    # Global mode may have no processPath. Recover socket owners before changing the rules.
    output = subprocess.check_output(["/bin/ps", "-axo", "pid=,comm="], text=True)
    pids = [parts[0] for line in output.splitlines() if len(parts := line.split(None, 1)) == 2
            and parts[1] == clash.ENGINE]
    if not pids:
        return set()
    listing = subprocess.run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(pids),
                              "-iTCP", "-sTCP:ESTABLISHED", "-Fn"], text=True, capture_output=True)
    return {tuple(line[1:].split("->", 1)) for line in listing.stdout.splitlines()
            if line.startswith("n") and "->" in line}


def old_codex_flows():
    endpoints = codex_tcp_endpoints()
    flows = set()
    for c in connections():
        m = c["metadata"]
        if clash.NAME in c.get("chains", []):
            continue
        source = f"{m['sourceIP']}:{m['sourcePort']}"
        # For fake-IP TUN sockets the controller omits destinationIP.
        attributed = m.get("processPath") == clash.ENGINE
        if not m.get("processPath") and str(m.get("network")).lower() == "tcp":
            attributed = any(src == source and dst.rsplit(":", 1)[-1] == str(m["destinationPort"])
                             and (not m.get("destinationIP") or dst == f"{m['destinationIP']}:{m['destinationPort']}")
                             for src, dst in endpoints)
        if attributed:
            flows.add(c["id"])
    return flows


def close_flows(ids):
    for cid in ids:
        clash.api("DELETE", "/connections/" + quote(cid, safe=""), timeout=5)


def restore_default():
    # Capture IDs before reload. Closing just our old streams forces the next turn to redial.
    used = [c["id"] for c in connections() if clash.NAME in c.get("chains", [])]
    clash.restore()
    close_flows(used)


def stop_requested(*args):
    global stopping
    stopping = True


def supervise(recover_only=False):
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = os.getppid()
    with (STATE / "switch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 2
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop_requested)
        problem = ""
        attempted = False
        try:
            if clash.live():
                restore_default()
            else:
                clash.restore()  # Finish a previous interrupted transaction, if present.
            if recover_only:
                write_status("off", "已恢复 Clash 原线路")
                return 0
            write_status("starting", "正在连接纽约 Hysteria 2…")
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            _, expected = clash.read_node_profile()
            stale = old_codex_flows()
            attempted = True
            clash.install(port)
            ok, detail = test_route(port)
            if not ok:
                raise RuntimeError(detail)
            if not stopping and os.getppid() == parent:
                close_flows(stale | old_codex_flows())
                write_status("on", f"Codex · 纽约 Hysteria 2 · {expected}", port)
            next_config = time.monotonic() + 2
            next_ip = time.monotonic() + 60
            misses = 0
            while not stopping and os.getppid() == parent:
                time.sleep(.4)
                now = time.monotonic()
                if now >= next_config:
                    if not clash.sync_mode(port):
                        raise RuntimeError("Clash 分流配置已变更，纽约开关已停止。")
                    next_config = now + 2
                if now >= next_ip:
                    ip = exit_ip(port)
                    if ip and ip != expected:
                        raise RuntimeError(f"出口已变为 {ip}，纽约开关已停止。")
                    misses = 0 if ip else misses + 1
                    if misses >= 3:
                        raise RuntimeError("连续三次未能验证纽约出口，已停止纽约模式。")
                    write_status("on" if ip else "reconnecting",
                                 f"Codex · 纽约 Hysteria 2 · {expected}" if ip else "正在重新验证纽约出口…",
                                 port)
                    next_ip = now + (60 if ip else 10)
        except Exception as exc:
            problem = str(exc)
        finally:
            if attempted:
                for attempt in range(3):
                    try:
                        restore_default()
                        break
                    except Exception:
                        time.sleep(1)
                else:
                    problem = (problem + " 无法确认已恢复原线路，请在 Clash Verge 重新载入当前配置。").strip()
            write_status("error" if problem else "off", problem or "已恢复 Clash 原线路")
        return 1 if problem else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--latency"]:
        try:
            interface, samples = measure_vps_latency()
            print(f"{statistics.median(samples):.0f} ms（中位数）")
            print(f"本机 {interface} → 纽约 VPS · ICMP 往返延迟")
            print(f"收到 {len(samples)}/5 个响应，实测为 " + "、".join(f"{t:.0f}" for t in samples) + " ms")
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(str(exc))
            sys.exit(1)
    elif sys.argv[1:2] == ["--test"]:
        try:
            ok, message = test_route(int(sys.argv[2]))
        except Exception:
            ok, message = False, "无法读取当前 Clash 分流状态。"
        print(message)
        sys.exit(0 if ok else 1)
    else:
        sys.exit(supervise(recover_only=sys.argv[1:] == ["--recover"]))
