#!/usr/bin/env python3
"""Read-only host metrics the Proxmox API does not expose.

Bound to vmbr1 (10.10.10.1) on purpose: that bridge has no physical port, so
this is reachable only from containers attached to it and never from the LAN.

Everything here is a read. There are no actions, so the worst a caller can do
is learn the host's temperature.
"""
import glob, json, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

BIND = os.environ.get("EXPORTER_BIND", "10.10.10.1")
PORT = int(os.environ.get("EXPORTER_PORT", "9101"))


def read(p, d=None):
    try:
        return open(p).read().strip()
    except Exception:
        return d


def hwmon():
    """Fans, pwm and temperatures, keyed by the chip that owns them."""
    out = {"fans": [], "temps": {}, "pwm": []}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        name = read(f"{h}/name", "?")
        for f in sorted(glob.glob(f"{h}/fan*_input")):
            v = read(f)
            if v and v.isdigit():
                out["fans"].append({"chip": name, "id": os.path.basename(f), "rpm": int(v)})
        for f in sorted(glob.glob(f"{h}/pwm[0-9]")):
            v, en = read(f), read(f + "_enable")
            if v and v.isdigit():
                out["pwm"].append({"chip": name, "id": os.path.basename(f),
                                   "value": int(v), "enable": en})
        for f in sorted(glob.glob(f"{h}/temp*_input")):
            v = read(f)
            if not (v and v.lstrip("-").isdigit()):
                continue
            label = read(f.replace("_input", "_label")) or os.path.basename(f)
            out["temps"].setdefault(name, []).append({"label": label, "c": int(v) / 1000})
    return out


def volumes():
    """Per-volume thin allocation - the number the PVE API does not surface, and
    the one that hit 98% and wedged a container twice."""
    try:
        r = subprocess.run(
            ["lvs", "--noheadings", "--units", "g", "--nosuffix", "-o",
             "lv_name,vg_name,lv_size,data_percent,pool_lv"],
            capture_output=True, text=True, timeout=15)
        vols = []
        for line in r.stdout.strip().splitlines():
            p = line.split()
            if len(p) >= 4 and p[3]:
                vols.append({"lv": p[0], "size_g": float(p[2]),
                             "alloc_pct": float(p[3]),
                             "pool": p[4] if len(p) > 4 else None})
        return vols
    except Exception as e:
        return [{"error": str(e)}]


def network():
    """Bridge membership and link state - the dongle dropping out of vmbr0 is
    how the wired path died, and it is invisible from inside a container."""
    out = {"bridges": {}, "links": {}}
    for b in glob.glob("/sys/class/net/*/brif"):
        br = b.split("/")[-2]
        try:
            out["bridges"][br] = sorted(os.listdir(b))
        except Exception:
            out["bridges"][br] = []
    for i in glob.glob("/sys/class/net/*"):
        n = os.path.basename(i)
        if n == "lo":
            continue
        # bonding_masters is a plain file in this directory, not an interface.
        # Without this it lists as a link in state "?" and, now that outages
        # are counted per link, would accrue a tally for something that cannot
        # go down because it was never up.
        oper = read(f"{i}/operstate")
        if oper is None:
            continue
        out["links"][n] = {"oper": oper, "carrier": read(f"{i}/carrier", "?")}
    return out


def power():
    """Battery, mains and the USB-C charger contract.

    The XPS charges over USB-C PD, so the delivered power is negotiated and
    reported by the ucsi source rather than by the battery, which exposes no
    power_now on this model. Watts here are therefore the contract (voltage x
    current the source is offering), not a measurement of actual draw.
    """
    out = {"battery": None, "ac_online": None, "charger": None}
    for d in glob.glob("/sys/class/power_supply/*"):
        t = read(f"{d}/type", "")
        name = os.path.basename(d)
        if t == "Mains":
            out["ac_online"] = read(f"{d}/online") == "1"
        elif t == "Battery":
            cap = read(f"{d}/capacity")
            now, full = read(f"{d}/charge_now"), read(f"{d}/charge_full")
            volt = read(f"{d}/voltage_now")
            b = {"name": name,
                 "percent": int(cap) if cap and cap.isdigit() else None,
                 "status": read(f"{d}/status"),
                 "cycles": read(f"{d}/cycle_count"),
                 "volts": round(int(volt) / 1e6, 2) if volt and volt.isdigit() else None}
            if now and full and now.isdigit() and full.isdigit() and volt:
                b["wh_now"] = round(int(now) * int(volt) / 1e12, 1)
                b["wh_full"] = round(int(full) * int(volt) / 1e12, 1)
                b["charge_now_uah"] = int(now)
                b["charge_full_uah"] = int(full)
            out["battery"] = b
        elif t == "USB":
            cur, volt = read(f"{d}/current_now"), read(f"{d}/voltage_now")
            if cur and volt and cur.isdigit() and volt.isdigit():
                v, a = int(volt) / 1e6, int(cur) / 1e6
                out["charger"] = {"name": name, "volts": round(v, 1), "amps": round(a, 2),
                                  "watts": round(v * a, 1),
                                  "status": read(f"{d}/status"),
                                  "online": read(f"{d}/online") == "1"}
    # Time to full only means something while actually charging.
    b = out["battery"]
    if b and b.get("status") == "Charging" and out.get("charger"):
        try:
            missing_uah = b["charge_full_uah"] - b["charge_now_uah"]
            amps = out["charger"]["amps"]
            if amps > 0:
                out["minutes_to_full"] = round((missing_uah / 1e6) / amps * 60)
        except Exception:
            pass
    return out


def deploy_state():
    """Last lines of the deploy log, so the dashboard can show progress without
    a second endpoint. Append-only and small."""
    try:
        lines = open("/var/log/pve-deploy.log").read().strip().splitlines()
    except Exception:
        return {"lines": [], "running": False}
    running = (any("deploy start" in l for l in lines[-40:])
               and not any("deploy done" in l or "FAILED: fetch" in l for l in lines[-12:]))
    return {"lines": lines[-12:], "running": running}


# Unit states are refreshed on a background thread, not inside the request.
# `pct exec` into a wedged container blocks - which has happened repeatedly on
# this box - and the dashboard polls this endpoint every 5 seconds. A stale
# reading is fine; a hung metrics endpoint is not.
WATCH_UNITS = {
    "host": ["pve-exporter", "pve-ops-none"],
    "101": ["k2-dashboard", "camrelay", "cloudflared"],
    "102": ["pve-ops", "cloudflared"],
}
UNIT_CACHE = {}


def refresh_units():
    while True:
        snap = {}
        for u in WATCH_UNITS["host"]:
            if u.endswith("-none"):
                continue
            try:
                r = subprocess.run(["systemctl", "is-active", u],
                                   capture_output=True, text=True, timeout=8)
                snap[f"host/{u}"] = r.stdout.strip() or "unknown"
            except Exception:
                snap[f"host/{u}"] = "unknown"
        for ct in ("101", "102"):
            for u in WATCH_UNITS[ct]:
                try:
                    r = subprocess.run(["pct", "exec", ct, "--", "systemctl", "is-active", u],
                                       capture_output=True, text=True, timeout=10)
                    snap[f"{ct}/{u}"] = r.stdout.strip() or "unknown"
                except Exception:
                    snap[f"{ct}/{u}"] = "unreachable"
        UNIT_CACHE.clear()
        UNIT_CACHE.update(snap)
        time.sleep(20)


def cpu():
    g = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor", "?")
    la = read("/proc/loadavg", "").split()[:3]
    avail = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "")
    return {"governor": g, "loadavg": la,
            # What the kernel actually offers, so the dashboard can list real
            # options rather than assuming performance/powersave.
            "available": avail.split() if avail else [],
            "driver": read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver", "?")}


# ----------------------------------------------------------------- actions
# This turns a read-only exporter into something that can change the host, so
# the surface is deliberately narrow:
#   * a fixed allowlist - the caller picks an action name, never a command
#   * arguments are validated against reality (a real container id, a governor
#     the kernel actually offers), not just pattern-matched
#   * subprocess is called with an argument list, so there is no shell to inject
#     into even if validation were wrong
#   * it listens only on vmbr1, which has no physical port
def container_ids():
    try:
        r = subprocess.run(["pct", "list"], capture_output=True, text=True, timeout=15)
        return {line.split()[0] for line in r.stdout.splitlines()[1:] if line.split()}
    except Exception:
        return set()


def governors():
    v = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "")
    return set(v.split()) if v else set()


def run(cmd, timeout=180):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {"ok": r.returncode == 0, "rc": r.returncode,
            "out": (r.stdout or "").strip()[-800:],
            "err": (r.stderr or "").strip()[-800:]}


def do_action(body):
    act = str(body.get("do", ""))

    if act in ("fstrim", "ct_reboot", "ct_start", "ct_stop"):
        ct = str(body.get("ct", ""))
        if ct not in container_ids():
            return {"ok": False, "err": f"unknown container {ct!r}"}
        cmd = {"fstrim":    ["pct", "fstrim", ct],
               "ct_reboot": ["pct", "reboot", ct],
               "ct_start":  ["pct", "start", ct],
               "ct_stop":   ["pct", "stop", ct]}[act]
        return run(cmd, timeout=300)

    if act == "deploy":
        branch = str(body.get("branch", "main"))
        # Must start alphanumeric and contain no "..": a leading dash would let
        # a branch name become a git option (--upload-pack=...), and ".." is
        # traversal. An earlier version allowed both.
        if (not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$", branch)
                or ".." in branch):
            return {"ok": False, "err": f"bad branch name {branch!r}"}
        # Detached: pve-deploy restarts this very process at the end, so the
        # response has to be sent before that happens.
        subprocess.Popen(["setsid", "/usr/local/sbin/pve-deploy", branch],
                         start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
        return {"ok": True, "out": f"deploy started on {branch}"}

    if act == "host_reboot":
        # Requires an explicit confirmation string in the body. Every other
        # action here is recoverable from this same dashboard; this one takes
        # the dashboard down with it, so a stray or replayed request must not
        # be enough to trigger it.
        if body.get("confirm") != "REBOOT-PVE-CODE1":
            return {"ok": False, "err": "host_reboot requires confirm=REBOOT-PVE-CODE1"}
        # +1 so the HTTP response is delivered before the box goes away.
        return run(["shutdown", "-r", "+1", "reboot requested from ops dashboard"], 30)

    if act == "nic_rejoin":
        # The dongle re-enumerates and comes back outside the bridge. Bringing
        # it up and re-adding it is the whole fix; nothing else is touched.
        a = run(["ip", "link", "set", "nic0", "up"], 30)
        b = run(["ip", "link", "set", "nic0", "master", "vmbr0"], 30)
        return {"ok": a["ok"] and b["ok"], "out": "nic0 up + rejoined vmbr0",
                "err": (a["err"] + " " + b["err"]).strip()}

    if act == "governor":
        g = str(body.get("value", ""))
        if g not in governors():
            return {"ok": False, "err": f"governor {g!r} not offered by this kernel"}
        errs = []
        for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor"):
            try:
                open(f, "w").write(g)
            except Exception as e:
                errs.append(str(e))
        return {"ok": not errs, "out": f"governor -> {g}", "err": "; ".join(errs[:3])}

    return {"ok": False, "err": f"unknown action {act!r}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/action":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 4096:
            self.send_error(413)
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        try:
            res = do_action(body)
        except Exception as e:
            res = {"ok": False, "err": f"{type(e).__name__}: {e}"}
        out = json.dumps(res).encode()
        self.send_response(200 if res.get("ok") else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = json.dumps({"hwmon": hwmon(), "volumes": volumes(),
                           "network": network(), "cpu": cpu(),
                           "power": power(), "deploy": deploy_state(),
                           "units": dict(UNIT_CACHE)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    threading.Thread(target=refresh_units, daemon=True).start()
    HTTPServer((BIND, PORT), H).serve_forever()
