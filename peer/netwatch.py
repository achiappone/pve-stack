#!/usr/bin/env python3
"""Internet watch for the home box.

  netwatch.py ping    every minute (netwatch-ping.timer): router, Cloudflare and
                      Google reachability plus a DNS lookup - a few hundred bytes
  netwatch.py speed   twice a day (netwatch-speed.timer) or on demand from the
                      dashboard: a full speedtest-cli run, about 450 MB, which is
                      why it is not hourly - Xfinity caps the line at 1.2 TB/month

Both append one JSON line to a file in NETWATCH_DIR; the exporter reads them.
"""
import json, os, re, socket, subprocess, sys, time

D = os.environ.get("NETWATCH_DIR", "/var/lib/netwatch")
EXTERNAL = [("cloudflare", "1.1.1.1"), ("google", "8.8.8.8")]
KEEP_PINGS = 7 * 1440          # a week of minutes
KEEP_SPEEDS = 120              # two months of twice-daily tests


def ping(host):
    try:
        r = subprocess.run(["ping", "-n", "-q", "-c", "3", "-W", "2", host],
                           capture_output=True, text=True, timeout=20)
        out = r.stdout
    except Exception:
        out = ""
    m = re.search(r"(\d+(?:\.\d+)?)% packet loss", out)
    loss = float(m.group(1)) if m else 100.0
    m = re.search(r"= [\d.]+/([\d.]+)/", out)
    return {"loss": loss, "rtt": float(m.group(1)) if m else None}


def gateway():
    """The router this box actually leaves through, from its own routing table:
    10.0.0.1 at home; at the office the default route moves to wifi when the
    wired port is on the lighting network, so a fixed address would be wrong."""
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
        return out.split()[2] if out.startswith("default via") else None
    except Exception:
        return None


def dns_ok():
    socket.setdefaulttimeout(3)
    try:
        socket.getaddrinfo("cloudflare.com", 443)
        return True
    except Exception:
        return False


def append(name, rec, keep, prune):
    """Append-only, pruned occasionally: rewriting a week of lines every minute
    would cost the SSD a couple of GB a day for nothing."""
    p = os.path.join(D, name)
    with open(p, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    if prune:
        lines = open(p).read().splitlines()[-keep:]
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, p)


def do_ping():
    gw = gateway()
    rec = {"t": int(time.time()), "dns": dns_ok(), "router_ip": gw}
    if gw:
        rec["router"] = ping(gw)
    for name, ip in EXTERNAL:
        rec[name] = ping(ip)
    append("pings.jsonl", rec, KEEP_PINGS, prune=time.localtime().tm_min == 0)


def do_speed():
    rec = {"t": int(time.time())}
    r = None
    try:
        r = subprocess.run(["speedtest-cli", "--json", "--secure"],
                           capture_output=True, text=True, timeout=240)
        d = json.loads(r.stdout)
        rec.update({"down": round(d["download"] / 1e6, 1), "up": round(d["upload"] / 1e6, 1),
                    "ping": round(d["ping"], 1), "server": d["server"]["sponsor"],
                    "where": d["server"]["name"], "isp": d["client"]["isp"],
                    "mb": round((d["bytes_received"] + d["bytes_sent"]) / 1e6)})
    except Exception as e:
        rec["error"] = ((r.stderr or "").strip()[-200:] if r else "") or str(e)[:200]
    append("speeds.jsonl", rec, KEEP_SPEEDS, prune=True)


if __name__ == "__main__":
    os.makedirs(D, exist_ok=True)
    {"ping": do_ping, "speed": do_speed}[sys.argv[1] if len(sys.argv) > 1 else "ping"]()
