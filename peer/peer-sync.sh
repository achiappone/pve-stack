#!/bin/bash
# Send this box's status and recent warnings to the other Proxmox box, every
# 5 minutes (peer-sync.timer). The other side keeps them in
# /var/log/peer/<this host>/, which does two jobs:
#   - its dashboard shows this box's health from status.json;
#   - the file's age is a heartbeat: its uptime-alert emails when this box
#     stops checking in;
#   - and if this box dies, the other one still holds its last logs.
#
# The key can only write into that one directory on the peer: the peer's
# authorized_keys pins it to `rrsync -wo -no-del` - no shell, no reads, no
# deletes. Same script on both boxes; /etc/peer-sync/env says which is which.
set -euo pipefail
. /etc/peer-sync/env            # PEER_HOST (the other box's SSH tunnel), SELF_DASH
# DRY_RUN=1 writes the bundle to $OUT_DIR (or a temp dir it prints) and stops.
if [ "${DRY_RUN:-0}" = 1 ]; then OUT=${OUT_DIR:-$(mktemp -d)}; mkdir -p "$OUT"
else OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT; fi
export SELF_DASH

# Machine-readable status for the peer's dashboard. pvesh rather than parsing
# `pct list`, so the fields are the API's own.
python3 - "$OUT/status.json" <<'PY'
import json, os, socket, subprocess, sys, time

def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout

def pv(path, *args):
    try:
        return json.loads(run("pvesh", "get", path, *args, "--output-format", "json") or "null")
    except Exception:
        return None

n = socket.gethostname()
st = pv(f"/nodes/{n}/status") or {}
tasks = pv(f"/nodes/{n}/tasks", "--typefilter", "vzdump", "--limit", "1") or []

# What this box's own uptime-alert currently counts as down, by name.
down = []
try:
    for line in open("/etc/uptime-alert.list"):
        parts = line.split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        key = "".join(c if c.isalnum() else "_" for c in parts[0])
        try:
            state = open(f"/var/lib/uptime-alert/{key}").read().split()
        except FileNotFoundError:
            continue                    # not checked yet
        if len(state) == 2 and state[1] == "1":
            down.append((parts[1].strip() if len(parts) > 1 else parts[0]))
except FileNotFoundError:
    pass

json.dump({
    "host": n,
    "sent": int(time.time()),
    "dashboard": os.environ.get("SELF_DASH", ""),
    "pve": (st.get("pveversion") or ""),
    "uptime_s": st.get("uptime"),
    "cpu": st.get("cpu"),
    "loadavg": st.get("loadavg"),
    "mem_used": (st.get("memory") or {}).get("used"),
    "mem_total": (st.get("memory") or {}).get("total"),
    "rootfs_used": (st.get("rootfs") or {}).get("used"),
    "rootfs_total": (st.get("rootfs") or {}).get("total"),
    "cts": [{"vmid": c.get("vmid"), "name": c.get("name"), "status": c.get("status")}
            for c in sorted(pv(f"/nodes/{n}/lxc") or [], key=lambda c: int(c.get("vmid", 0)))],
    "storage": [{"storage": s.get("storage"), "used": s.get("used"), "total": s.get("total"),
                 "active": s.get("active")} for s in (pv(f"/nodes/{n}/storage") or [])],
    "last_backup": ({"status": tasks[0].get("status"), "start": tasks[0].get("starttime"),
                     "end": tasks[0].get("endtime")} if tasks else None),
    "failed_units": [l.split()[0] for l in run("systemctl", "--failed", "--no-legend", "--plain").splitlines() if l.strip()],
    "alerts_down": down,
}, open(sys.argv[1], "w"))
PY

# Human-readable, for reading after the fact.
journalctl -p warning --since -24h --no-pager -o short-iso 2>/dev/null | tail -n 400 > "$OUT/journal.txt" || true

[ "${DRY_RUN:-0}" = 1 ] && { echo "dry run: bundle in $OUT"; exit 0; }

# -t keeps the mtimes - status.json's is the heartbeat the peer checks.
rsync -rt --timeout=120 -e "ssh -i /etc/peer-sync/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes \
-o UserKnownHostsFile=/etc/peer-sync/known_hosts -o StrictHostKeyChecking=accept-new \
-o 'ProxyCommand=/usr/local/bin/cloudflared access ssh --hostname %h'" \
  "$OUT/" "root@$PEER_HOST:"
