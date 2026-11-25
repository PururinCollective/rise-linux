#!/usr/bin/python3 -I
"""RISE Linux WebGUI: a small HTTPS server (Python standard library only).

  server.py                       serve on WEBGUI_PORT (18080), IPv4 and IPv6
  server.py --set-password USER   set the login (the password is read from stdin)

The WebGUI runs as root because it changes the firewall, DHCP and services;
every command it runs is an argument list (no shell), and every value from the
browser is checked against a pattern before it is written anywhere.

Who may use it: one user, /etc/rise/secrets/webgui.json (scrypt). A login gives
an HttpOnly, Secure, SameSite=Strict session cookie; every POST also needs the
session's CSRF token in X-CSRF-Token. Five failed logins from one address
block it for five minutes. The firewall keeps VPN clients away entirely.
"""
import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")
CONF = "/etc/rise/rise.conf"
CONF_DEFAULT = "/etc/rise/rise.conf.default"
SECRETS = "/etc/rise/secrets"
AUTH_FILE = f"{SECRETS}/webgui.json"
PORTFWD = "/etc/rise/portfwd.conf"
DHCP_STATIC = "/etc/rise/dhcp-static.conf"
LEASES = "/var/lib/rise/dnsmasq.leases"
NETPLAN = "/etc/netplan/50-rise.yaml"
RUN = "/run/rise"
RATES = "/run/rise/rates.json"
HISTORY = "/var/lib/rise/history.json"
VPNCMD = "/opt/softether/bin/vpncmd"
SESSION_TTL = 8 * 3600

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ttf": "font/ttf", ".woff2": "font/woff2", ".json": "application/json", ".ico": "image/x-icon"}


# ---- settings -----------------------------------------------------------------------

def read_conf():
    c = {}
    for path in (CONF_DEFAULT, CONF):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    try:
                        parts = shlex.split(v, comments=True)
                    except ValueError:
                        continue
                    c[k.strip()] = parts[0] if parts else ""
        except OSError:
            pass
    return c


def write_conf(changes):
    try:
        lines = open(CONF).read().splitlines()
    except OSError:
        lines = open(CONF_DEFAULT).read().splitlines()
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Z0-9_]+)=", line)
        if m and m.group(1) in changes:
            k = m.group(1)
            out.append(f'{k}="{changes[k]}"')
            seen.add(k)
        else:
            out.append(line)
    for k, v in changes.items():
        if k not in seen:
            out.append(f'{k}="{v}"')
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, CONF)


# ---- passwords and sessions ------------------------------------------------------------

def hash_password(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt$15$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def check_password(pw, stored):
    try:
        _, n, r, p, salt, h = stored.split("$")
        salt, h = base64.b64decode(salt), base64.b64decode(h)
        got = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(h))
        return hmac.compare_digest(got, h)
    except (ValueError, TypeError):
        return False


def set_password(user, pw):
    os.makedirs(SECRETS, mode=0o700, exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"user": user, "hash": hash_password(pw)}, f)
    os.replace(tmp, AUTH_FILE)


SESSIONS = {}          # token -> {"csrf", "expires", "user"}
FAILS = {}             # ip -> [count, first_time]
LOCK = threading.Lock()


def login_blocked(ip):
    with LOCK:
        n, t = FAILS.get(ip, [0, 0])
        if time.time() - t > 300:
            FAILS.pop(ip, None)
            return False
        return n >= 5


def login_failed(ip):
    with LOCK:
        n, t = FAILS.get(ip, [0, time.time()])
        FAILS[ip] = [n + 1, t if n else time.time()]


# ---- running things -------------------------------------------------------------------------

def run(args, timeout=30, input=None):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           input=input, stdin=None if input is not None else subprocess.DEVNULL)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def vpncmd(command, hub=False, timeout=20):
    """One vpncmd command as the server administrator, CSV output parsed."""
    rc, rows, _ = vpncmd_text(command, hub, timeout)
    return rc, rows


def vpncmd_text(command, hub=False, timeout=20):
    """vpncmd commands (one per line, one admin session: SoftEther refuses
    bursts of admin logins), as (rc, CSV rows, text)."""
    c = read_conf()
    try:
        pw = open(f"{SECRETS}/softether-admin").read().strip()
    except OSError:
        return 1, []
    port = (c.get("SOFTETHER_TCP_PORTS", "5555").split() or ["5555"])[0]
    path = f"{RUN}/webgui-vpncmd.{secrets.token_hex(6)}"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(command + "\n")
    args = [VPNCMD, "/SERVER", f"localhost:{port}", f"/PASSWORD:{pw}", "/CSV", f"/IN:{path}"]
    if hub:
        args.insert(3, f"/ADMINHUB:{c.get('VPN_HUB', 'VPN')}")
    try:
        rc, out = run(args, timeout=timeout)
    finally:
        os.unlink(path)
    import csv
    import io
    # before the CSV, vpncmd says where its commands come from and echoes the
    # prompt with the command; neither is part of the table
    lines = [l for l in out.splitlines()
             if l.strip() and not l.startswith("The commands written")
             and not re.match(r"^VPN Server[^>]*>", l)]
    return rc, list(csv.reader(io.StringIO("\n".join(lines)))), "\n".join(lines)


def vpncmd_error(text):
    """The reason in vpncmd's output ("Error occurred. (Error code: 51)" and the line after)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if l.startswith("Error occurred"):
            return " ".join(lines[i:i + 2])
    return lines[-1] if lines else "SoftEther did not answer"


def vq(v):
    """A vpncmd parameter value: quoted (spaces), "" for a quote."""
    return '"' + v.replace('"', '""') + '"'


def systemctl_state(unit):
    rc, out = run(["systemctl", "is-active", unit], timeout=5)
    return out.splitlines()[0] if out else "unknown"


# ---- data ------------------------------------------------------------------------------------

def read_file(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def os_release():
    d = {}
    for line in read_file("/etc/os-release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v.strip('"')
    return d


def addresses(ifname):
    rc, out = run(["ip", "-j", "addr", "show", "dev", ifname], timeout=5)
    try:
        info = json.loads(out)[0]
    except (ValueError, IndexError):
        return {"up": False, "v4": [], "v6": [], "mac": ""}
    v4 = [f'{a["local"]}/{a["prefixlen"]}' for a in info.get("addr_info", []) if a.get("family") == "inet"]
    v6 = [f'{a["local"]}/{a["prefixlen"]}' for a in info.get("addr_info", [])
          if a.get("family") == "inet6" and a.get("scope") == "global"]
    speed = read_file(f"/sys/class/net/{ifname}/speed").strip()
    return {"up": "UP" in info.get("flags", []) and info.get("operstate") != "DOWN",
            "v4": v4, "v6": v6, "mac": info.get("address", ""), "mtu": info.get("mtu"),
            "speed": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else None}


def meminfo():
    m = {}
    for line in read_file("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0]) * 1024 if v.split() else 0
    return {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0),
            "buffers": m.get("Buffers", 0), "cached": m.get("Cached", 0),
            "swap_total": m.get("SwapTotal", 0), "swap_free": m.get("SwapFree", 0)}


CPU_PREV = [None]


def cpu_percent():
    f = read_file("/proc/stat").splitlines()[0].split()[1:]
    vals = list(map(int, f))
    idle, total = vals[3] + vals[4], sum(vals)
    prev = CPU_PREV[0]
    CPU_PREV[0] = (idle, total)
    if not prev or total == prev[1]:
        return None
    return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)


def cert_info(domain):
    path = f"/etc/letsencrypt/live/{domain}/cert.pem"
    if not domain or not os.path.exists(path):
        return {"present": False}
    rc, out = run(["openssl", "x509", "-in", path, "-noout", "-enddate", "-issuer"], timeout=5)
    end = re.search(r"notAfter=(.*)", out)
    issuer = re.search(r"issuer=(.*)", out)
    days = None
    if end:
        try:
            t = time.mktime(time.strptime(end.group(1).strip(), "%b %d %H:%M:%S %Y %Z"))
            days = int((t - time.time()) // 86400)
        except ValueError:
            pass
    return {"present": True, "expires": end.group(1).strip() if end else "", "days": days,
            "issuer": issuer.group(1).strip() if issuer else ""}


def overview():
    c = read_conf()
    osr = os_release()
    rel = {}
    for line in read_file("/etc/rise/release").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            rel[k] = v.strip('"')
    up = float(read_file("/proc/uptime", "0 0").split()[0])
    st = os.statvfs("/")
    wan, tap = c.get("WAN_IF", "eth0"), "tap_" + c.get("VPN_TAP", "vpn")
    units = ["rise-vpnserver", "rise-dnsmasq", "rise-firewall", "rise-webgui", "rise-stats", "ssh"]
    return {
        "hostname": socket.gethostname(),
        "rise": f'{rel.get("RISE_NAME", "RISE Linux")} {rel.get("RISE_VERSION", "")}'.strip(),
        "build": rel.get("RISE_BUILD", ""),
        "os": osr.get("PRETTY_NAME", ""),
        "kernel": os.uname().release,
        "arch": os.uname().machine,
        "cpu": next((l.split(":", 1)[1].strip() for l in read_file("/proc/cpuinfo").splitlines()
                     if l.startswith("model name")), ""),
        "cpus": os.cpu_count(),
        "cpu_percent": cpu_percent(),
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "uptime": up,
        "load": os.getloadavg(),
        "memory": meminfo(),
        "disk": {"total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize},
        "softether": read_file("/opt/softether/VERSION").strip(),
        "wan": dict(addresses(wan), name=wan),
        "vpn": dict(addresses(tap), name=tap),
        "services": {u: systemctl_state(u) for u in units},
        "fullcone": {"v4": read_file(f"{RUN}/fullcone4").strip() == "1",
                     "v6": read_file(f"{RUN}/fullcone6").strip() == "1"},
        "firstboot": os.path.exists("/var/lib/rise/firstboot-pending"),
        "domain": c.get("VPN_DOMAIN", ""),
        "tap_mac": c.get("VPN_TAP_MAC", ""),
        "cert": cert_info(c.get("VPN_DOMAIN", "")),
        "conf": {k: c.get(k, "") for k in ("VPN_IPV4", "VPN_IPV6", "VPN_IPV6_ENABLE", "VPN_MTU",
                                           "NAT66", "NAT_FULLCONE", "VPN_PROXY_ARP", "VPN_ISOLATION",
                                           "SSH_PORT", "WEBGUI_PORT")},
    }


def sessions():
    rc, rows = vpncmd("SessionList", hub=True)
    if rc != 0 or not rows:
        return {"ok": False, "sessions": []}
    head, out = rows[0], []
    for r in rows[1:]:
        if len(r) == len(head):
            out.append(dict(zip(head, r)))
    return {"ok": True, "columns": head, "sessions": out}


def se_time(v):
    """SoftEther's "2026-12-02 (Wed) 16:00:00" (server local time) as a UNIX time, else None."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2}) \(\w+\) (\d{2}):(\d{2}):(\d{2})$", (v or "").strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss = map(int, m.groups())
    return int(time.mktime((y, mo, d, hh, mi, ss, 0, 0, -1)))


def vpn_users():
    rc, rows = vpncmd("UserList", hub=True)
    if rc != 0 or not rows:
        return {"ok": False, "users": []}
    head = rows[0]
    users = [dict(zip(head, r)) for r in rows[1:] if len(r) == len(head)]
    for u in users:
        u["expires_ts"] = se_time(u.get("Expiration Date"))
    return {"ok": True, "columns": head, "users": users, "now": int(time.time()),
            "tz": time.strftime("%Z")}


def leases():
    out = []
    for line in read_file(LEASES).splitlines():
        f = line.split()
        if len(f) >= 4 and f[0].isdigit():
            out.append({"expires": int(f[0]), "mac": f[1], "ip": f[2], "name": "" if f[3] == "*" else f[3]})
        elif len(f) >= 4 and f[0] != "duid":
            out.append({"expires": int(f[0]) if f[0].isdigit() else 0, "mac": "", "ip": f[2], "name": f[3]})
    return out


def table_read(path, ncols):
    rows = []
    for line in read_file(path).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        f = line.split(None, ncols - 1)
        rows.append(f + [""] * (ncols - len(f)))
    return rows


def table_write(path, header, rows):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(header)
        for r in rows:
            f.write(" ".join(str(x) for x in r).rstrip() + "\n")
    os.replace(tmp, path)


# ---- validation ---------------------------------------------------------------------------

RE_MAC = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
RE_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
RE_NAME = re.compile(r"^[A-Za-z0-9._-]{0,63}$")
RE_PORTS = re.compile(r"^\d{1,5}(-\d{1,5})?$")
RE_NOTE = re.compile(r"^[^\n\r\"$`\\]{0,80}$")
RE_DUR = re.compile(r"^\d+[smhd]?$|^infinite$")


class Bad(Exception):
    pass


def need(cond, msg):
    if not cond:
        raise Bad(msg)


def ports_ok(p):
    if not RE_PORTS.match(p or ""):
        return False
    a = [int(x) for x in p.split("-")]
    return all(1 <= x <= 65535 for x in a) and (len(a) == 1 or a[0] < a[1])


def ip_in(ip, net):
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_interface(net).network
    except ValueError:
        return False


def is_iface(v, version):
    try:
        i = ipaddress.ip_interface(v)
        return i.version == version and 0 < i.network.prefixlen < (32 if version == 4 else 128)
    except ValueError:
        return False


def is_ip(v, version=None):
    try:
        a = ipaddress.ip_address(v)
        return version is None or a.version == version
    except ValueError:
        return False


def onoff(v):
    return "1" if v in (True, 1, "1", "true", "on", "yes") else "0"


# ---- actions ---------------------------------------------------------------------------------

def act_settings(d):
    """VPN, NAT and firewall settings: validated, written, applied."""
    c = read_conf()
    ch = {}
    if "VPN_IPV4" in d:
        need(is_iface(d["VPN_IPV4"], 4), "VPN IPv4: an address with a prefix (10.255.255.1/24)")
        ch["VPN_IPV4"] = d["VPN_IPV4"]
    net4 = ch.get("VPN_IPV4", c.get("VPN_IPV4"))
    for k in ("VPN_DHCP_START", "VPN_DHCP_END"):
        if k in d:
            need(is_ip(d[k], 4) and ip_in(d[k], net4), f"{k}: an address inside {net4}")
            ch[k] = d[k]
    if "VPN_IPV6" in d:
        need(is_iface(d["VPN_IPV6"], 6), "VPN IPv6: an address with a prefix (fd00:255::1/64)")
        ch["VPN_IPV6"] = d["VPN_IPV6"]
    for k, ver in (("VPN_DNS4", 4), ("VPN_DNS6", 6)):
        if k in d:
            vals = d[k].split()
            need(len(vals) <= 4 and all(is_ip(x, ver) for x in vals), f"{k}: IPv{ver} addresses")
            ch[k] = " ".join(vals)
    if "VPN_MTU" in d:
        need(str(d["VPN_MTU"]).isdigit() and 1280 <= int(d["VPN_MTU"]) <= 1500, "MTU: 1280 to 1500")
        ch["VPN_MTU"] = str(int(d["VPN_MTU"]))
    if "VPN_DHCP_LEASE" in d:
        need(RE_DUR.match(d["VPN_DHCP_LEASE"]), "lease time: 12h, 30m, 3600 or infinite")
        ch["VPN_DHCP_LEASE"] = d["VPN_DHCP_LEASE"]
    for k in ("VPN_IPV6_ENABLE", "VPN_PROXY_ARP", "VPN_ISOLATION", "VPN_ALLOW_PING", "VPN_BLOCK_LAN",
              "NAT_FULLCONE", "NAT66", "SOFTETHER_UDP_ACCEL", "SOFTETHER_VPN_OVER_DNS", "SOFTETHER_VPN_OVER_ICMP"):
        if k in d:
            ch[k] = onoff(d[k])
    if not ch:
        return {"ok": True, "message": "nothing changed"}
    write_conf(ch)
    rc, out = run(["/usr/sbin/rise", "apply"], timeout=90)
    return {"ok": rc == 0, "message": "applied" if rc == 0 else out[-500:]}


def act_portfwd(d):
    rows = table_read(PORTFWD, 7)
    op = d.get("op")
    if op == "add":
        fam = str(d.get("family", "4"))
        need(fam in ("4", "6"), "family: 4 or 6")
        proto = d.get("proto", "tcp")
        need(proto in ("tcp", "udp", "both"), "protocol: tcp, udp or both")
        ext = str(d.get("port", ""))
        need(ports_ok(ext), "external port: 1-65535 or a range a-b")
        dst = d.get("dest", "")
        c = read_conf()
        net = c.get("VPN_IPV4") if fam == "4" else c.get("VPN_IPV6")
        need(is_ip(dst, int(fam)) and ip_in(dst, net), f"destination: an IPv{fam} address inside {net}")
        dport = str(d.get("dport", "") or "-")
        need(dport == "-" or ports_ok(dport), "destination port: 1-65535 or a range")
        if "-" in ext and dport != "-":
            a, b = map(int, ext.split("-"))
            need("-" in dport and (lambda x, y: y - x)(*map(int, dport.split("-"))) == b - a,
                 "a port range forwards to a range of the same size (or leave it empty)")
        local = {c.get("SSH_PORT"), c.get("WEBGUI_PORT"), "80"} | set(c.get("SOFTETHER_TCP_PORTS", "").split())
        if proto in ("tcp", "both") and "-" not in ext:
            need(ext not in local, f"tcp/{ext} is used by this server")
        note = d.get("note", "")
        need(RE_NOTE.match(note), "the note has characters it cannot have")
        for r in rows:
            if r[0] == fam and r[2] == ext and (r[1] == proto or "both" in (r[1], proto)):
                raise Bad(f"IPv{fam} {proto} {ext} is already forwarded")
        rows.append([fam, proto, ext, dst, dport, "1", note])
    elif op in ("delete", "toggle"):
        i = int(d.get("index", -1))
        need(0 <= i < len(rows), "no such rule")
        if op == "delete":
            rows.pop(i)
        else:
            rows[i][5] = "0" if rows[i][5] == "1" else "1"
    else:
        raise Bad("op: add, delete or toggle")
    table_write(PORTFWD, "# family proto port dest dport enabled note (rise firewall)\n", rows)
    rc, out = run(["/usr/lib/rise/firewall", "reload"], timeout=30)
    return {"ok": rc == 0, "message": "firewall reloaded" if rc == 0 else out[-500:]}


def portfwd_list():
    return [{"index": i, "family": r[0], "proto": r[1], "port": r[2], "dest": r[3], "dport": r[4],
             "enabled": r[5] == "1", "note": r[6]} for i, r in enumerate(table_read(PORTFWD, 7))]


def act_dhcp(d):
    rows = table_read(DHCP_STATIC, 4)
    op = d.get("op")
    c = read_conf()
    if op == "add":
        mac = d.get("mac", "").lower()
        need(RE_MAC.match(mac), "MAC address: aa:bb:cc:dd:ee:ff")
        ip4 = d.get("ip", "") or "-"
        need(ip4 == "-" or (is_ip(ip4, 4) and ip_in(ip4, c.get("VPN_IPV4"))), f"IPv4: inside {c.get('VPN_IPV4')}")
        gw4 = c.get("VPN_IPV4", "").split("/")[0]
        need(ip4 != gw4, "that is the gateway's address")
        ip6 = d.get("ip6", "") or "-"
        need(ip6 == "-" or (is_ip(ip6, 6) and ip_in(ip6, c.get("VPN_IPV6"))), f"IPv6: inside {c.get('VPN_IPV6')}")
        name = d.get("name", "") or "-"
        need(name == "-" or RE_HOST.match(name.lower()), "hostname: letters, digits, -")
        need(ip4 != "-" or ip6 != "-", "give an IPv4 or an IPv6 address")
        for r in rows:
            need(r[0] != mac, f"{mac} already has a static lease")
            need(ip4 == "-" or r[1] != ip4, f"{ip4} is already reserved")
        rows.append([mac, ip4, ip6, name])
    elif op == "delete":
        i = int(d.get("index", -1))
        need(0 <= i < len(rows), "no such lease")
        rows.pop(i)
    else:
        raise Bad("op: add or delete")
    table_write(DHCP_STATIC, "# mac ipv4 ipv6 hostname ('-' for none)\n", rows)
    rc, out = run(["systemctl", "reload", "rise-dnsmasq.service"], timeout=20)
    return {"ok": rc == 0, "message": "DHCP reloaded" if rc == 0 else out[-500:]}


def dhcp_list():
    return {"static": [{"index": i, "mac": r[0], "ip": r[1], "ip6": r[2], "name": r[3]}
                       for i, r in enumerate(table_read(DHCP_STATIC, 4))],
            "leases": leases()}


def network_get():
    c = read_conf()
    wan = c.get("WAN_IF", "eth0")
    text = read_file(NETPLAN)
    cfg = {"iface": wan, "v4": "dhcp", "v6": "auto", "addr4": "", "gw4": "", "addr6": "", "gw6": "", "dns": "",
           "token6": ""}
    m = re.search(r'ipv6-address-token:\s*"?([0-9a-fA-F:]+)"?', text)
    if m:
        cfg["token6"] = m.group(1)
    if "dhcp4: false" in text:
        cfg["v4"] = "static"
    if "link-local: [ ipv4 ]" in text:
        cfg["v6"] = "off"
    elif "dhcp6: false" in text:
        cfg["v6"] = "static"
    for a in re.findall(r'^\s+- "([0-9a-fA-F:.]+/\d+)"', text, re.M):
        cfg["addr6" if ":" in a else "addr4"] = a
    for g in re.findall(r'via: "([0-9a-fA-F:.]+)"', text):
        cfg["gw6" if ":" in g else "gw4"] = g
    dns = re.search(r"nameservers:\s*\n\s*addresses:\s*\n((?:\s+- .*\n?)+)", text)
    if dns:
        cfg["dns"] = " ".join(re.findall(r'- "?([0-9a-fA-F:.]+)"?', dns.group(1)))
    cfg["live"] = addresses(wan)
    return cfg


def act_network(d):
    c = read_conf()
    wan = c.get("WAN_IF", "eth0")
    v4, v6 = d.get("v4"), d.get("v6")
    need(v4 in ("dhcp", "static") and v6 in ("auto", "static", "off"), "mode")
    addrs, routes, dns = [], [], (d.get("dns") or "").split()
    need(all(is_ip(x) for x in dns) and len(dns) <= 6, "DNS: IP addresses")
    if v4 == "static":
        need(is_iface(d.get("addr4", ""), 4), "IPv4 address/prefix")
        need(is_ip(d.get("gw4", ""), 4) and ip_in(d["gw4"], d["addr4"]), "IPv4 gateway inside the subnet")
        addrs.append(d["addr4"]); routes.append(d["gw4"])
    if v6 == "static":
        need(is_iface(d.get("addr6", ""), 6), "IPv6 address/prefix")
        need(is_ip(d.get("gw6", ""), 6), "IPv6 gateway")
        addrs.append(d["addr6"]); routes.append(d["gw6"])
    need(v4 == "dhcp" or dns, "a static IPv4 needs DNS servers")
    token6 = (d.get("token6") or "").strip() if v6 == "auto" else ""
    if token6:
        try:
            t = int(ipaddress.IPv6Address(token6))
        except ValueError:
            t = 0
        need(0 < t < 2 ** 64, "IPv6 token: the interface part only, for example ::45")
    mac = addresses(wan).get("mac", "")
    y = ["# written by the RISE WebGUI", "network:", "  version: 2", "  renderer: networkd",
         "  ethernets:", f"    {wan}:"]
    if mac:
        y += ["      match:", f'        macaddress: "{mac}"', f"      set-name: {wan}"]
    y.append(f"      dhcp4: {'true' if v4 == 'dhcp' else 'false'}")
    if v6 == "auto":
        y += ["      dhcp6: true", "      accept-ra: true"]
        if token6:
            y.append(f'      ipv6-address-token: "{token6}"')
    elif v6 == "static":
        y += ["      dhcp6: false", "      accept-ra: false"]
    else:
        y += ["      dhcp6: false", "      accept-ra: false", "      link-local: [ ipv4 ]"]
    if addrs:
        y.append("      addresses:")
        y += [f'        - "{a}"' for a in addrs]
        y.append("      routes:")
        for g in routes:
            y += ["        - to: default", f'          via: "{g}"']
    if dns:
        y += ["      nameservers:", "        addresses:"] + [f'          - "{a}"' for a in dns]
    old = read_file(NETPLAN)
    fd = os.open(NETPLAN + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(y) + "\n")
    os.replace(NETPLAN + ".tmp", NETPLAN)
    rc, out = run(["netplan", "generate"], timeout=30)
    if rc != 0:
        fd = os.open(NETPLAN, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w") as f:
            f.write(old)
        return {"ok": False, "message": "netplan rejected it: " + out[-400:]}
    write_conf({"NAT66": "1" if v6 != "off" else "0",
                "VPN_IPV6_ENABLE": "1" if v6 != "off" else "0"})
    # apply after the answer is sent: the address may change under this page
    threading.Timer(1.0, lambda: (run(["netplan", "apply"], timeout=60), run(["/usr/sbin/rise", "apply"], timeout=90))).start()
    return {"ok": True, "message": "applying; if the address changed, open the WebGUI at the new one"}


def act_service(d):
    units = {"vpnserver": "rise-vpnserver.service", "dhcp": "rise-dnsmasq.service",
             "firewall": "rise-firewall.service", "webgui": "rise-webgui.service", "ssh": "ssh.service",
             "stats": "rise-stats.service"}
    u = units.get(d.get("name"))
    need(u, "unknown service")
    action = d.get("action", "restart")
    need(action in ("restart", "start", "stop"), "action")
    need(not (u in ("rise-webgui.service", "rise-firewall.service") and action == "stop"),
         "stopping this would cut the WebGUI off")
    if u == "rise-webgui.service":
        threading.Timer(1.0, lambda: run(["systemctl", "restart", u])).start()
        return {"ok": True, "message": "restarting the WebGUI"}
    if u == "rise-firewall.service" and action == "restart":
        rc, out = run(["/usr/lib/rise/firewall", "reload"], timeout=30)
    else:
        rc, out = run(["systemctl", action, u], timeout=60)
    return {"ok": rc == 0, "message": f"{u}: {action}" if rc == 0 else out[-500:]}


def act_system(d):
    a = d.get("action")
    if a in ("reboot", "poweroff"):
        threading.Timer(1.5, lambda: run(["systemctl", a])).start()
        return {"ok": True, "message": f"{a} in a moment"}
    if a == "hostname":
        h = d.get("hostname", "").lower()
        need(RE_HOST.match(h), "hostname: lowercase letters, digits and -")
        rc, out = run(["hostnamectl", "set-hostname", h])
        if rc == 0:
            hosts = re.sub(r"^127\.0\.1\.1\s.*$", f"127.0.1.1 {h}", read_file("/etc/hosts"), flags=re.M)
            with open("/etc/hosts", "w") as f:
                f.write(hosts)
        return {"ok": rc == 0, "message": out or "hostname changed"}
    if a == "console":
        m = d.get("mode", "")
        need(m == "native" or re.match(r"^\d{3,4}x\d{3,4}$", m), "mode: WxH or native")
        rc, out = run(["/usr/lib/rise/console", "mode", m], timeout=120)
        return {"ok": rc == 0, "message": "from the next boot" if rc == 0 else out[-400:]}
    if a == "fontscale":
        s = d.get("scale", "auto")
        need(s in ("auto", "1", "2"), "scale: auto, 1 or 2")
        write_conf({"CONSOLE_FONT_SCALE": s})
        run(["/usr/lib/rise/console", "font"])
        return {"ok": True, "message": "console font set"}
    if a == "timezone":
        tz = d.get("timezone", "")
        need(re.match(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$", tz) and os.path.exists(f"/usr/share/zoneinfo/{tz}"),
             "unknown time zone")
        rc, out = run(["timedatectl", "set-timezone", tz])
        return {"ok": rc == 0, "message": out or f"time zone {tz}"}
    raise Bad("unknown action")


def act_cert(d):
    a = d.get("action")
    if a == "domain":
        dom = d.get("domain", "").lower()
        need(re.match(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", dom), "a domain name")
        mail = d.get("email", "")
        need(mail == "" or re.match(r"^[^@\s\"$`\\]+@[^@\s\"$`\\]+\.[^@\s\"$`\\]+$", mail), "e-mail address")
        write_conf({"VPN_DOMAIN": dom, "ACME_EMAIL": mail})
        # port 80 opens only with a domain (Let's Encrypt's HTTP check)
        run(["/usr/lib/rise/firewall", "reload"], timeout=30)
        a = "obtain"
    need(a in ("obtain", "renew"), "action")
    rc, out = run(["/usr/lib/rise/cert", a], timeout=180)
    return {"ok": rc == 0, "message": out[-800:] or ("done" if rc == 0 else "failed")}


def weak_psk(psk):
    """Short, digits only, or a usual word with digits after it (vpn, vpn123, password1)."""
    return len(psk) < 8 or bool(re.fullmatch(
        r"(vpn|password|passw0rd|qwerty|softether|ipsec|secret|changeme|admin|letmein|test)?[0-9]*",
        psk.lower()))


def act_vpn(d):
    a = d.get("action")
    if a == "disconnect":
        name = d.get("session", "")
        need(re.match(r"^[A-Za-z0-9_.@-]{1,128}$", name), "session name")
        rc, rows = vpncmd(f"SessionDisconnect {name}", hub=True)
        return {"ok": rc == 0, "message": "disconnected" if rc == 0 else "failed"}
    if a == "psk":
        psk = d.get("psk", "")
        need(re.match(r"^[A-Za-z0-9._@%+=-]{1,64}$", psk), "pre-shared key: 1-64 of A-Z a-z 0-9 ._@%+=-")
        rc, out = run(["/usr/lib/rise/softether-setup", "psk", psk], timeout=40)
        if rc != 0:
            return {"ok": False, "message": out[-400:]}
        msg = "IPsec key changed"
        if weak_psk(psk):
            msg += (". Warning: it is easy to guess; whoever knows it can pose as this server"
                    " to L2TP/IPsec clients. A long random key is safer.")
        return {"ok": True, "message": msg}
    raise Bad("unknown action")


RE_VPNUSER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,62}$")
RE_TEXT = re.compile(r"^[^\x00-\x1f\x7f\"]{0,127}$")
RE_VPNPASS = re.compile(r"^[^\x00-\x1f\x7f\"]{1,255}$")


def expires_arg(e):
    """{"mode": "never"} | {"mode": "days", "days": N} | {"mode": "at", "at": UNIX time}
    as UserExpiresSet's /EXPIRES (server local time)."""
    e = e or {}
    mode = e.get("mode", "never")
    if mode == "never":
        return "none", None
    if mode == "days":
        days = e.get("days")
        need(isinstance(days, int) and 1 <= days <= 3650, "days: 1 to 3650")
        ts = int(time.time()) + days * 86400
    elif mode == "at":
        ts = e.get("at")
        need(isinstance(ts, (int, float)), "expiry date")
        ts = int(ts)
        need(ts > time.time(), "the expiry date is in the past")
        need(ts < time.time() + 50 * 365 * 86400, "the expiry date is too far away")
    else:
        raise Bad("expiry")
    return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(ts)), ts


def pass_note(pw):
    return " (a short password: easy to guess)" if len(pw) < 8 else ""


def act_vpnuser(d):
    """VPN users of the hub: add, delete, password, expiry."""
    a = d.get("action")
    name = d.get("name", "")
    need(isinstance(name, str) and RE_VPNUSER.match(name),
         "user name: 1-63 of A-Z a-z 0-9 ._@- (starting with a letter or digit)")
    if a == "add":
        pw = d.get("password", "")
        realname = (d.get("realname") or "").strip()
        note = (d.get("note") or "").strip()
        need(isinstance(pw, str) and RE_VPNPASS.match(pw), "password: 1-255 characters, no double quote")
        need(RE_TEXT.match(realname), "full name: up to 127 characters, no double quote")
        need(RE_TEXT.match(note), "description: up to 127 characters, no double quote")
        exp, ts = expires_arg(d.get("expires"))
        rc, rows = vpncmd("UserList", hub=True)
        need(rc == 0, "SoftEther did not answer")
        need(not any(r and r[0].lower() == name.lower() for r in rows[1:]), f"{name} exists already")
        # UserCreate gives the user a random password, so a failure halfway
        # never leaves an account anyone can log in to
        cmds = [f"UserCreate {name} /GROUP:none /REALNAME:{vq(realname or 'none')} /NOTE:{vq(note or 'none')}",
                f"UserPasswordSet {name} /PASSWORD:{vq(pw)}"]
        if ts:
            cmds.append(f"UserExpiresSet {name} /EXPIRES:{vq(exp)}")
        rc, _, out = vpncmd_text("\n".join(cmds), hub=True)
        if rc != 0:
            return {"ok": False, "message": f"could not add {name}: {vpncmd_error(out)}"}
        return {"ok": True, "message": f"{name} added" + (f", expires {exp} {time.strftime('%Z')}" if ts else "") + pass_note(pw)}
    if a == "delete":
        rc, _, out = vpncmd_text(f"UserDelete {name}", hub=True)
        if rc != 0:
            return {"ok": False, "message": vpncmd_error(out)}
        # whoever is still connected as this user goes too
        rc, rows = vpncmd("SessionList", hub=True)
        if rc == 0 and rows:
            head = rows[0]
            gone = [r[0] for r in rows[1:] if len(r) == len(head) and dict(zip(head, r)).get("User Name") == name]
            if gone:
                vpncmd_text("\n".join(f"SessionDisconnect {s}" for s in gone), hub=True)
        return {"ok": True, "message": f"{name} deleted"}
    if a == "password":
        pw = d.get("password", "")
        need(isinstance(pw, str) and RE_VPNPASS.match(pw), "password: 1-255 characters, no double quote")
        rc, _, out = vpncmd_text(f"UserPasswordSet {name} /PASSWORD:{vq(pw)}", hub=True)
        if rc != 0:
            return {"ok": False, "message": vpncmd_error(out)}
        return {"ok": True, "message": f"password of {name} changed" + pass_note(pw)}
    if a == "expires":
        exp, ts = expires_arg(d.get("expires"))
        rc, _, out = vpncmd_text(f"UserExpiresSet {name} /EXPIRES:{vq(exp)}", hub=True)
        if rc != 0:
            return {"ok": False, "message": vpncmd_error(out)}
        return {"ok": True, "message": f"{name} expires {exp} {time.strftime('%Z')}" if ts else f"{name} never expires"}
    raise Bad("unknown action")


def act_password(d, user):
    auth = json.load(open(AUTH_FILE))
    need(check_password(d.get("current", ""), auth["hash"]), "the current password is wrong")
    new = d.get("new", "")
    need(len(new) >= 8, "at least 8 characters")
    newuser = d.get("user") or auth["user"]
    need(re.match(r"^[A-Za-z0-9._-]{1,32}$", newuser), "user name")
    set_password(newuser, new)
    with LOCK:
        SESSIONS.clear()
    return {"ok": True, "message": "password changed; log in again"}


def logs(unit, n=200):
    units = {"vpnserver": "rise-vpnserver", "dhcp": "rise-dnsmasq", "firewall": "rise-firewall",
             "webgui": "rise-webgui", "cert": "rise-cert", "firstboot": "rise-firstboot",
             "ssh": "ssh", "kernel": None, "system": "*"}
    need(unit in units, "unknown log")
    args = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(min(max(n, 10), 1000))]
    if unit == "kernel":
        args.append("-k")
    elif unit != "system":
        args += ["-u", units[unit]]
    rc, out = run(args, timeout=15)
    return {"ok": True, "text": out}


# ---- HTTP -----------------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RISE"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    def headers_common(self):
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.headers_common()
        self.end_headers()
        self.wfile.write(body)

    def session(self):
        m = re.search(r"(?:^|;\s*)rise_session=([A-Za-z0-9_-]+)", self.headers.get("Cookie", ""))
        if not m:
            return None
        with LOCK:
            s = SESSIONS.get(m.group(1))
            if not s or s["expires"] < time.time():
                SESSIONS.pop(m.group(1), None)
                return None
            s["expires"] = time.time() + SESSION_TTL
            return dict(s, token=m.group(1))

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise Bad("request too large")
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise Bad("not JSON")
        if not isinstance(d, dict):
            raise Bad("not an object")
        return d

    def static(self, path):
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        if rel.startswith("..") or rel.startswith("/"):
            return self.send_error(404)
        full = os.path.join(STATIC, rel)
        if not os.path.isfile(full):
            return self.send_error(404)
        data = open(full, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400" if "/fonts/" in full else "no-cache")
        self.headers_common()
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self.static(u.path)
        s = self.session()
        if u.path == "/api/session":
            return self.send_json({"user": s["user"], "csrf": s["csrf"]} if s else {"user": None})
        if not s:
            return self.send_json({"error": "login"}, 401)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/overview":
                return self.send_json(overview())
            if u.path == "/api/rates":
                return self.send_json(json.loads(read_file(RATES, "{}") or "{}"))
            if u.path == "/api/history":
                return self.send_json(json.loads(read_file(HISTORY, "[]") or "[]"))
            if u.path == "/api/sessions":
                return self.send_json(sessions())
            if u.path == "/api/users":
                return self.send_json(vpn_users())
            if u.path == "/api/settings":
                return self.send_json(read_conf())
            if u.path == "/api/portfwd":
                return self.send_json(portfwd_list())
            if u.path == "/api/dhcp":
                return self.send_json(dhcp_list())
            if u.path == "/api/network":
                return self.send_json(network_get())
            if u.path == "/api/firewall":
                rc, out = run(["/usr/lib/rise/firewall", "show"], timeout=15)
                return self.send_json({"text": out})
            if u.path == "/api/logs":
                return self.send_json(logs(q.get("unit", ["vpnserver"])[0], int(q.get("n", ["200"])[0])))
            if u.path == "/api/psk":
                psk = read_file(f"{SECRETS}/ipsec-psk").strip()
                return self.send_json({"psk": psk, "weak": weak_psk(psk)})
        except Bad as e:
            return self.send_json({"ok": False, "message": str(e)}, 400)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        ip = self.client_address[0]
        try:
            if u.path == "/api/login":
                if login_blocked(ip):
                    return self.send_json({"ok": False, "message": "too many attempts; wait five minutes"}, 429)
                d = self.body_json()
                try:
                    auth = json.load(open(AUTH_FILE))
                except (OSError, ValueError):
                    return self.send_json({"ok": False, "message": "no WebGUI user is set (rise webgui-passwd)"}, 500)
                ok_user = hmac.compare_digest(str(d.get("user", "")), auth.get("user", ""))
                ok_pw = check_password(str(d.get("password", "")), auth.get("hash", ""))
                if not (ok_user and ok_pw):
                    login_failed(ip)
                    time.sleep(1)
                    return self.send_json({"ok": False, "message": "wrong user or password"}, 403)
                tok, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                with LOCK:
                    for k in [k for k, v in SESSIONS.items() if v["expires"] < time.time()]:
                        SESSIONS.pop(k)
                    SESSIONS[tok] = {"user": auth["user"], "csrf": csrf, "expires": time.time() + SESSION_TTL}
                    FAILS.pop(ip, None)
                return self.send_json({"ok": True, "user": auth["user"], "csrf": csrf},
                                      cookie=f"rise_session={tok}; Path=/; HttpOnly; Secure; SameSite=Strict")
            s = self.session()
            if not s:
                return self.send_json({"error": "login"}, 401)
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), s["csrf"]):
                return self.send_json({"error": "csrf"}, 403)
            if u.path == "/api/logout":
                with LOCK:
                    SESSIONS.pop(s["token"], None)
                return self.send_json({"ok": True}, cookie="rise_session=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
            d = self.body_json()
            routes = {"/api/settings": act_settings, "/api/portfwd": act_portfwd, "/api/dhcp": act_dhcp,
                      "/api/network": act_network, "/api/service": act_service, "/api/system": act_system,
                      "/api/cert": act_cert, "/api/vpn": act_vpn, "/api/vpnuser": act_vpnuser}
            if u.path == "/api/password":
                return self.send_json(act_password(d, s["user"]))
            if u.path in routes:
                r = routes[u.path](d)
                return self.send_json(r, 200 if r.get("ok") else 500)
            return self.send_json({"error": "not found"}, 404)
        except Bad as e:
            return self.send_json({"ok": False, "message": str(e)}, 400)
        except Exception as e:  # report, keep serving
            sys.stderr.write(f"error: {e!r}\n")
            return self.send_json({"ok": False, "message": "internal error"}, 500)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    address_family = socket.AF_INET6
    tls = None

    def server_bind(self):
        # one socket for IPv4 and IPv6
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def finish_request(self, request, client_address):
        # the TLS handshake happens here, in the request's own thread, so a
        # slow or silent client never holds up the accept loop
        request.settimeout(15)
        try:
            request = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            request.close()
            return
        request.settimeout(60)
        super().finish_request(request, client_address)


def tls_context():
    c = read_conf()
    dom = c.get("VPN_DOMAIN", "")
    pairs = [(f"/etc/letsencrypt/live/{dom}/fullchain.pem", f"/etc/letsencrypt/live/{dom}/privkey.pem"),
             (f"{SECRETS}/webgui-selfsigned.crt", f"{SECRETS}/webgui-selfsigned.key")]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    for crt, key in pairs:
        if dom or "selfsigned" in crt:
            try:
                ctx.load_cert_chain(crt, key)
                sys.stderr.write(f"certificate: {crt}\n")
                return ctx
            except (OSError, ssl.SSLError):
                continue
    # last resort: make a self-signed one now
    os.makedirs(SECRETS, mode=0o700, exist_ok=True)
    run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
         "-days", "3650", "-subj", f"/CN={dom or socket.gethostname()}",
         "-keyout", pairs[1][1], "-out", pairs[1][0]], timeout=30)
    ctx.load_cert_chain(*pairs[1])
    return ctx


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--set-password":
        user = sys.argv[2]
        if not re.match(r"^[A-Za-z0-9._-]{1,32}$", user):
            sys.exit("user name: letters, digits and ._-")
        pw = sys.stdin.read().rstrip("\n")
        if len(pw) < 8:
            sys.exit("the password needs at least 8 characters")
        set_password(user, pw)
        print(f"WebGUI login set for {user}")
        return
    os.makedirs(RUN, exist_ok=True)
    port = int(read_conf().get("WEBGUI_PORT", "18080"))
    httpd = Server(("::", port), Handler)
    httpd.tls = tls_context()
    sys.stderr.write(f"RISE WebGUI on https://[::]:{port}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
