#!/usr/bin/env python3
"""Internet watch for the home box.

  netwatch.py ping    every minute (netwatch-ping.timer): router, Cloudflare and
                      Google reachability plus a DNS lookup - a few hundred bytes
  netwatch.py mirror  every 3 hours (netwatch-mirror.timer): push a 16 MB file to
                      the other Proxmox box through its tunnel and time it - this
                      box's upload into the other box's download, the real path
                      between the two sites; the other box measures the reverse
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


PROBE_MB = 16


def do_mirror():
    """Time a 16 MB push to the peer over the same SSH-through-Cloudflare path
    peer-sync uses, with the same write-only key. -I forces the transfer even
    though the file is unchanged; random data defeats any compression."""
    env = dict(l.strip().split("=", 1) for l in open("/etc/peer-sync/env") if "=" in l and not l.startswith("#"))
    probe = os.path.join(D, "probe.bin")
    if not os.path.exists(probe) or os.path.getsize(probe) != PROBE_MB << 20:
        with open(probe, "wb") as f:
            f.write(os.urandom(PROBE_MB << 20))
    ssh = ("ssh -i /etc/peer-sync/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes "
           "-o UserKnownHostsFile=/etc/peer-sync/known_hosts -o StrictHostKeyChecking=accept-new "
           "-o 'ProxyCommand=/usr/local/bin/cloudflared access ssh --hostname %h'")
    rec = {"t": int(time.time()), "kind": "push", "to": env.get("PEER_HOST", "?"), "mb": PROBE_MB}
    t0 = time.monotonic()
    try:
        r = subprocess.run(["rsync", "-I", "--whole-file", "--timeout=120", "-e", ssh, probe, f"root@{env['PEER_HOST']}:"],
                           capture_output=True, text=True, timeout=300)
        secs = time.monotonic() - t0
        if r.returncode == 0:
            rec.update({"seconds": round(secs, 2), "mbps": round(PROBE_MB * 8 * 1.048576 / secs, 1)})
        else:
            rec["error"] = (r.stderr or "").strip()[-200:] or f"rsync exit {r.returncode}"
    except Exception as e:
        rec["error"] = str(e)[:200]
    append("mirror.jsonl", rec, 400, prune=True)


if __name__ == "__main__":
    os.makedirs(D, exist_ok=True)
    {"ping": do_ping, "speed": do_speed, "mirror": do_mirror}[sys.argv[1] if len(sys.argv) > 1 else "ping"]()
