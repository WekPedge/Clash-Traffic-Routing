#!/usr/bin/env python3
"""Install the pinned Hysteria 2 service or export its Mihomo client profile."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import pwd
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

CONFIG = Path("/etc/codex-ny-hy2")
BINDIR = Path("/usr/local/lib/codex-ny-hy2")
UNIT = Path("/etc/systemd/system/codex-ny-hy2.service")
USER = "codex-hy2"
SNI = "ny-hy2.test"
RELEASE = "https://github.com/HyNetworks/hysteria/releases/download/app/v2.14.0/hysteria-linux-amd64"
SHA256 = "fc25edff9948708cb00a4ff01f3bccde677660aca050133bb1ad46c6860362d2"
DOMAINS = ("chatgpt.com", "openai.com", "oaistatic.com", "oaiusercontent.com",
           "api.ipify.org", "www.cloudflare.com", "cp.cloudflare.com", "www.gstatic.com")


def server_config():
    allowed = " || ".join(f'suffix("{domain}")' for domain in DOMAINS)
    return {
        "listen": "0.0.0.0:443",
        "tls": {"cert": str(CONFIG / "server.crt"), "key": str(CONFIG / "server.key"), "sniGuard": "strict"},
        "auth": {"type": "password", "password": secrets.token_urlsafe(40)},
        "acl": {"rules": [{"outbound": "direct", "match": f'proto == "tcp" && dst.port == 443 && ({allowed})'},
                          {"outbound": "reject"}]},
    }


def export_profile(server, egress):
    cfg = json.loads((CONFIG / "server.yaml").read_text())
    if cfg.get("auth", {}).get("type") != "password" or not cfg["auth"].get("password"):
        raise ValueError("Expected this project's password-authenticated server configuration")
    if cfg.get("listen") != "0.0.0.0:443":
        raise ValueError("Expected this project's UDP 443 listener")
    cert = Path(cfg["tls"]["cert"])
    subprocess.run(["openssl", "verify", "-CAfile", cert, "-verify_hostname", SNI, cert],
                   check=True, stdout=subprocess.DEVNULL)
    der = subprocess.check_output(["openssl", "x509", "-in", cert, "-outform", "DER"])
    proxy = {"name": "CodexNY-Hysteria2", "type": "hysteria2", "server": server, "port": 443,
             "sni": SNI, "password": cfg["auth"]["password"], "fingerprint": hashlib.sha256(der).hexdigest(),
             "skip-cert-verify": False}
    profile = {"expected-egress-ip": egress or server, "proxies": [proxy]}
    # mkstemp creates a root-owned 0600 file. A failed export leaves the previous profile intact.
    target = CONFIG / "client-profile.yaml"
    with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG, delete=False) as f:
        tmp = Path(f.name)
    try:
        tmp.write_text(json.dumps(profile, indent=2) + "\n")
        tmp.replace(target)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"Client profile written to {target} (0600)")


def install(server, egress):
    binary = BINDIR / "hysteria"
    if all(path.is_file() for path in (UNIT, binary, CONFIG / "server.yaml", CONFIG / "server.crt", CONFIG / "server.key")):
        export_profile(server, egress)
        print("Existing installation retained")
        return
    if any(os.path.lexists(path) for path in (CONFIG, UNIT, BINDIR)):
        raise RuntimeError("Partial or conflicting installation. Inspect the existing service paths before retrying.")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("This installer ships the Linux x86_64 release")
    if subprocess.check_output(["ss", "-H", "-lun", "sport = :443"]).strip():
        raise RuntimeError("UDP port 443 is already in use")
    with tempfile.TemporaryDirectory(prefix="codex-ny-hy2-") as stage:
        asset = Path(stage) / "hysteria"
        subprocess.run(["curl", "-qfLsS", "--retry", "2", "--max-time", "90", "-o", asset, RELEASE], check=True)
        if hashlib.sha256(asset.read_bytes()).hexdigest() != SHA256:
            raise RuntimeError("Downloaded binary does not match the pinned SHA-256")
        try:
            account = pwd.getpwnam(USER)
        except KeyError:
            subprocess.run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", USER],
                           check=True)
            account = pwd.getpwnam(USER)
        if account.pw_uid == 0:
            raise RuntimeError("The service account must be unprivileged")

        # Record only paths created by this invocation. Never delete a pre-existing installation on failure.
        created_dirs = []
        unit_created = False
        try:
            CONFIG.mkdir(mode=0o750)
            created_dirs.append(CONFIG)
            os.chown(CONFIG, 0, account.pw_gid)
            CONFIG.chmod(0o750)
            BINDIR.mkdir(mode=0o755)
            created_dirs.append(BINDIR)
            BINDIR.chmod(0o755)
            shutil.copyfile(asset, binary)
            binary.chmod(0o755)
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                            "-nodes", "-sha256", "-days", "730", "-subj", f"/CN={SNI}",
                            "-addext", f"subjectAltName=DNS:{SNI},IP:{server}",
                            "-keyout", CONFIG / "server.key", "-out", CONFIG / "server.crt"], check=True)
            (CONFIG / "server.yaml").write_text(json.dumps(server_config(), indent=2) + "\n")
            for name in ("server.key", "server.crt", "server.yaml"):
                os.chown(CONFIG / name, 0, account.pw_gid)
                (CONFIG / name).chmod(0o640)
            export_profile(server, egress)
            unit_text = Path(__file__).with_name(UNIT.name).read_text()
            with UNIT.open("x") as f:
                unit_created = True
                f.write(unit_text)
            UNIT.chmod(0o644)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "--now", UNIT.name], check=True)
            time.sleep(1)
            subprocess.run(["systemctl", "is-active", "--quiet", UNIT.name], check=True)
        except BaseException:
            if unit_created:
                subprocess.run(["systemctl", "disable", "--now", UNIT.name], check=False)
                UNIT.unlink(missing_ok=True)
                subprocess.run(["systemctl", "daemon-reload"], check=False)
            for directory in reversed(created_dirs):
                shutil.rmtree(directory)
            raise
    print(f"{UNIT.name} is active on UDP 443")


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError("Expected a literal IPv4 address") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "export"])
    parser.add_argument("server", type=ipv4, help="public IPv4 address clients connect to")
    parser.add_argument("--expected-egress-ip", type=ipv4, help="public egress IPv4 if different from the server address")
    args = parser.parse_args()
    if os.getuid() != 0:
        parser.error("Run as root on the VPS")
    os.umask(0o077)
    with Path("/run/codex-ny-hy2-install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (install if args.action == "install" else export_profile)(args.server, args.expected_egress_ip)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
