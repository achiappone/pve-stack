#!/usr/bin/env python3
"""K2 printer watchdog. Runs on the HOST once a minute (k2-watch.timer).

Finds the K2 Plus by its Moonraker hostname wherever it is - the lighting
network (wired, normal) or the office wifi (its fallback when the cable loses
link; it sat there from about 2026-10-30 to 11-03 with both dashboards blind)
- and keeps the K2 dashboard (CT 101) and the ops dashboard (CT 102) pointed
at it:

  lighting network  the containers reach it directly, so only the address in
                    their env files changes. Forwarding on the host stays OFF.
  office wifi       the containers cannot reach that network, so the host
                    routes them to the printer's address and ONLY that
                    address: each CT gets a /32 route via the host, the host
                    forwards it out wifi behind its own wifi address (NAT),
                    and everything else routed between the networks is
                    dropped. Nothing on the wifi can reach the lighting
                    network or vmbr1. It is plain routing, not a port relay,
                    so the WebRTC camera and the ssh repair actions work too.

Every move is emailed. A printer that is simply off is not a move: the last
address is kept and nothing changes.

Search order: known lighting addresses, a sweep of the lighting /24 (at most
every 10 min), known wifi addresses, a sweep of the wifi /24 (at most every
6 h: that is the office network, and frequent sweeps would look like a port
scan). Lighting first, so a printer back on its cable leaves the wifi route.

  k2-watch.py              one check (the timer runs this)
  k2-watch.py --selfcheck  test the search order without touching anything
"""
import concurrent.futures as cf
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HOSTNAME = os.environ.get("K2_HOSTNAME", "K2Plus-05E6")
LAN_IF = os.environ.get("LAN_IF", "vmbr0")
WIFI_IF = os.environ.get("WIFI_IF", "wlp59s0")
LAN_VIA = os.environ.get("LAN_VIA", "2.0.0.43")   # the host on the lighting network
STATE = "/var/lib/k2-watch/state.json"
LAN_SWEEP_S, WIFI_SWEEP_S = 600, 6 * 3600
# Who talks to the printer, and how each one's config names it.
CTS = {
    "101": ("/etc/k2-dashboard.env", "PRINTER_HOST", "{ip}", ["k2-dashboard", "camrelay"]),
    "102": ("/etc/pve-ops.env", "PRINTER_URL", "http://{ip}:7125", ["pve-ops"]),
}


def sh(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def ct(vmid, *cmd):
    return sh("lxc-attach", "-n", vmid, "--", *cmd)


def is_printer(ip):
    """By identity, not just an open port: another Moonraker box must not win."""
    try:
        with urllib.request.urlopen(f"http://{ip}:7125/printer/info", timeout=3) as r:
            return json.load(r)["result"].get("hostname") == HOSTNAME
    except Exception:
        return False


def port_open(ip):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex((ip, 7125)) == 0
    finally:
        s.close()


def sweep(ifname):
    """The printer's address in the /24 around this interface's first IPv4."""
    f = sh("ip", "-4", "-o", "addr", "show", "dev", ifname).stdout.split()
    if "inet" not in f:
        return None
    net = ipaddress.ip_network(f"{f[f.index('inet') + 1].split('/')[0]}/24", strict=False)
    hosts = [str(h) for h in net.hosts()]
    with cf.ThreadPoolExecutor(64) as ex:
        opened = [h for h, ok in zip(hosts, ex.map(port_open, hosts)) if ok]
    return next((h for h in opened if is_printer(h)), None)


def on_lan(ip):
    return f" dev {LAN_IF} " in f" {sh('ip', 'route', 'get', ip).stdout} "


def locate(st, now, probe, sweep_lan, sweep_wifi, lan):
    """Pure search, so --selfcheck can drive it. Records sweep times in st."""
    known = st["known"]
    for group, sweeper, key, every in ((
            [k for k in known if lan(k)], sweep_lan, "lan", LAN_SWEEP_S), (
            [k for k in known if not lan(k)], sweep_wifi, "wifi", WIFI_SWEEP_S)):
        for ip in group:
            if probe(ip):
                return ip
        if now - st["swept"].get(key, 0) >= every:
            st["swept"][key] = now
            ip = sweeper()
            if ip:
                return ip
    return None


def wifi_path(ip, st):
    """Route the CTs to a wifi printer through the host (ip), or undo it (None).
    Rules go in before forwarding comes on, and forwarding goes off before the
    rules come out, so there is never a moment of open forwarding."""
    have = st.get("routed")
    if ip and have == ip and sh("iptables", "-C", "K2WATCH", "-i", LAN_IF, "-o", WIFI_IF,
                                "-d", ip, "-j", "ACCEPT").returncode == 0:
        for vmid in CTS:  # a restarted CT loses its route; cheap to re-add
            ct(vmid, "ip", "route", "replace", f"{ip}/32", "via", LAN_VIA, "dev", "eth0")
        return
    if not ip:
        for i in (LAN_IF, WIFI_IF):
            sh("sysctl", "-q", "-w", f"net.ipv4.conf.{i}.forwarding=0")
    for table, parent in (("filter", "FORWARD"), ("nat", "POSTROUTING")):
        sh("iptables", "-t", table, "-N", "K2WATCH")
        if sh("iptables", "-t", table, "-C", parent, "-j", "K2WATCH").returncode:
            sh("iptables", "-t", table, "-I", parent, "1", "-j", "K2WATCH")
        sh("iptables", "-t", table, "-F", "K2WATCH")
    if have:
        for vmid in CTS:
            ct(vmid, "ip", "route", "del", f"{have}/32")
    st["routed"] = None
    if not ip:
        return
    rule = lambda *r: sh("iptables", "-A", "K2WATCH", *r)
    rule("-i", LAN_IF, "-o", WIFI_IF, "-d", ip, "-j", "ACCEPT")
    rule("-i", WIFI_IF, "-o", LAN_IF, "-s", ip, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    rule("-i", WIFI_IF, "-j", "DROP")
    # "! -o LAN_IF" keeps bridged traffic out of this, should br_netfilter
    # ever be loaded: bridged frames show as in=vmbr0 out=vmbr0.
    rule("-i", LAN_IF, "!", "-o", LAN_IF, "-j", "DROP")
    sh("iptables", "-t", "nat", "-A", "K2WATCH", "-o", WIFI_IF, "-d", ip, "-j", "MASQUERADE")
    for i in (LAN_IF, WIFI_IF):
        sh("sysctl", "-q", "-w", f"net.ipv4.conf.{i}.forwarding=1")
    for vmid in CTS:
        ct(vmid, "ip", "route", "replace", f"{ip}/32", "via", LAN_VIA, "dev", "eth0")
    st["routed"] = ip


def repoint(ip):
    for vmid, (path, key, fmt, units) in CTS.items():
        val = fmt.format(ip=ip)
        ct(vmid, "sed", "-i", f"s#^{key}=.*#{key}={val}#", path)
        ct(vmid, "systemctl", "try-restart", *units)


def notify(old, new, wifi):
    where = "the office wifi" if wifi else "the lighting network"
    body = (f"The K2 printer ({HOSTNAME}) stopped answering at {old} and was found at "
            f"{new} on {where}. The K2 and ops dashboards now point at {new}.\n")
    if wifi:
        body += ("\nIt is on wifi because its network cable has no link. The containers reach it "
                 "through pve-code1 until it is back on the lighting network; check the cable.\n")
    msg = f"To: root\nSubject: K2 printer moved to {new} ({'wifi' if wifi else 'lighting network'})\n\n{body}"
    subprocess.run(["/usr/sbin/sendmail", "-t"], input=msg, text=True)


def main():
    try:
        st = json.load(open(STATE))
    except (OSError, ValueError):
        cur = ct("101", "sed", "-n", "s/^PRINTER_HOST=//p", "/etc/k2-dashboard.env").stdout.strip() or "2.0.0.125"
        st = {"ip": cur, "known": [cur], "swept": {}, "routed": None, "missing": False}
    ip = locate(st, time.time(), is_printer, lambda: sweep(LAN_IF), lambda: sweep(WIFI_IF), on_lan)
    if not ip:
        if not st["missing"]:
            print(f"printer not found; keeping {st['ip']} (switched off?)", flush=True)
        st["missing"] = True
    else:
        if st["missing"]:
            print(f"printer answering at {ip}", flush=True)
        st["missing"] = False
        if ip not in st["known"]:
            st["known"].append(ip)
        wifi = not on_lan(ip)
        wifi_path(ip if wifi else None, st)
        if ip != st["ip"]:
            print(f"printer moved {st['ip']} -> {ip} ({'wifi' if wifi else 'lighting network'}); dashboards repointed", flush=True)
            repoint(ip)
            notify(st["ip"], ip, wifi)
            st["ip"] = ip
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump(st, open(STATE, "w"))


def selfcheck():
    lan = lambda ip: ip.startswith("2.")
    calls = []
    def run(alive, known, swept=None, now=100_000, lan_hit=None, wifi_hit=None):
        st = {"known": known, "swept": dict(swept or {})}
        sl = lambda: calls.append("lan") or lan_hit
        sw = lambda: calls.append("wifi") or wifi_hit
        return locate(st, now, lambda ip: ip in alive, sl, sw, lan), st
    assert run({"2.0.0.125", "10.20.5.28"}, ["10.20.5.28", "2.0.0.125"])[0] == "2.0.0.125", "lighting wins"
    assert run({"10.20.5.28"}, ["2.0.0.125", "10.20.5.28"], swept={"lan": 99_900})[0] == "10.20.5.28", "wifi fallback"
    calls.clear()
    assert run(set(), ["2.0.0.125"], lan_hit="2.0.0.77")[0] == "2.0.0.77" and calls == ["lan"], "new lighting address"
    calls.clear()
    ip, st = run(set(), ["2.0.0.125"], wifi_hit="10.20.5.28")
    assert ip == "10.20.5.28" and calls == ["lan", "wifi"] and st["swept"] == {"lan": 100_000, "wifi": 100_000}, "first wifi sweep"
    calls.clear()
    assert run(set(), ["2.0.0.125"], swept={"lan": 99_900, "wifi": 90_000})[0] is None and calls == [], "off: no sweeps due"
    calls.clear()
    assert run({"10.20.5.28", "2.0.0.80"}, ["10.20.5.28"], lan_hit="2.0.0.80")[0] == "2.0.0.80", "cable back: leave wifi"
    print("all checks passed")


if __name__ == "__main__":
    selfcheck() if sys.argv[1:] == ["--selfcheck"] else main()
