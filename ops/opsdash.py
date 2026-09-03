#!/usr/bin/env python3
"""Proxmox ops dashboard.

Deliberately not a clone of the PVE web UI. It surfaces the things that have
actually gone wrong on this box, which the stock UI either buries or does not
show at all:

  * per-volume thin allocation - the PVE API only reports pool usage, so a
    container volume creeping to 98% while the pool sits at 1% is invisible
    there. That wedged container 101 twice in one day.
  * bridge membership - the USB ethernet dongle re-enumerated and came back
    outside vmbr0, which killed the wired path with no error anywhere.
  * temperatures and fans - not in the PVE API at all.

Data comes from two places: the Proxmox API over a read-only token, and a small
host exporter on vmbr1 for what the API cannot answer.
"""
import json, os, secrets, ssl, urllib.request, urllib.parse, urllib.error
import threading, time
import http.server, socketserver

ENV = {}
for line in open("/etc/pve-ops.env"):
    if "=" in line and not line.startswith("#"):
        k, v = line.strip().split("=", 1)
        ENV[k] = v

PVE_HOST = ENV.get("PVE_HOST", "https://127.0.0.1:8006")
PVE_NODE = ENV.get("PVE_NODE", "pve")
TOKEN_ID = ENV.get("PVE_TOKEN_ID", "")
TOKEN_SECRET = ENV.get("PVE_TOKEN_SECRET", "")
EXPORTER = os.environ.get("EXPORTER_URL", "http://10.10.10.1:9101/metrics")
EXPORTER_ACTION = EXPORTER.replace("/metrics", "/action")
# Mirrored from the exporter's own allowlist. Duplicated on purpose: the
# exporter is the security boundary, but rejecting here too means a typo in the
# page cannot even reach it.
ALLOWED_ACTIONS = {"fstrim", "ct_reboot", "ct_start", "ct_stop", "nic_rejoin",
                   "governor", "host_reboot"}
PORT = int(os.environ.get("OPS_PORT", "8780"))
AUTH_USER = os.environ.get("OPS_USER", "")
AUTH_PASS = os.environ.get("OPS_PASS", "")
LOGIN_REQUIRED = bool(AUTH_USER and AUTH_PASS)
SESSIONS = set()

# The node presents its own CA. Verification is off for this one internal call;
# the token is the credential and the hop never leaves the host.
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def pve(path):
    r = urllib.request.Request(
        PVE_HOST + "/api2/json" + path,
        headers={"Authorization": f"PVEAPIToken={TOKEN_ID}={TOKEN_SECRET}"})
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        return json.load(resp)["data"]


def metrics():
    with urllib.request.urlopen(EXPORTER, timeout=10) as r:
        return json.load(r)


PROBE_EVERY = int(os.environ.get("OPS_PROBE_SECONDS", "60"))
PROBES = {}


def probe_one(url):
    """Reachability, not correctness.

    Any HTTP response counts as up - a 401 from a login-gated dashboard means
    the server is answering, which is the question being asked. Only a refused
    connection, DNS failure or timeout counts as down.
    """
    t0 = time.time()
    try:
        req = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "pve-ops/1.0"})
        with urllib.request.urlopen(req, timeout=8, context=CTX) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code                      # answered, just not with 200
    except Exception as e:
        return {"up": False, "err": type(e).__name__, "ms": round((time.time()-t0)*1000)}
    return {"up": True, "code": code, "ms": round((time.time()-t0)*1000)}


def probe_loop():
    while True:
        for sv in SERVICES:
            u = sv.get("url")
            if u:
                try:
                    PROBES[u] = probe_one(u)
                except Exception as e:
                    PROBES[u] = {"up": False, "err": str(e)[:60]}
        time.sleep(PROBE_EVERY)


def act(body):
    if body.get("do") not in ALLOWED_ACTIONS:
        return 400, {"ok": False, "err": f"action {body.get('do')!r} not allowed"}
    req = urllib.request.Request(EXPORTER_ACTION, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return 200, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {"ok": False, "err": f"HTTP {e.code}"}
    except Exception as e:
        return 502, {"ok": False, "err": f"{type(e).__name__}: {e}"}


def snapshot():
    """(services are static, but travel with the snapshot so the page polls once)"""
    """Everything the page needs, in one call, so the client polls once."""
    out = {"ok": True, "errors": [], "services": SERVICES, "probes": PROBES}
    try:
        out["node"] = pve(f"/nodes/{PVE_NODE}/status")
    except Exception as e:
        out["errors"].append(f"node status: {e}")
    try:
        out["storage"] = pve(f"/nodes/{PVE_NODE}/storage")
    except Exception as e:
        out["errors"].append(f"storage: {e}")
    try:
        out["disks"] = pve(f"/nodes/{PVE_NODE}/disks/list")
    except Exception as e:
        out["errors"].append(f"disks: {e}")
    try:
        out["lxc"] = sorted(pve(f"/nodes/{PVE_NODE}/lxc"), key=lambda c: c["vmid"])
    except Exception as e:
        out["errors"].append(f"lxc: {e}")
    try:
        out["host"] = metrics()
    except Exception as e:
        out["errors"].append(f"exporter: {e}")
    return out


# Which container serves what. Not derivable from the PVE API - it knows the
# containers exist but nothing about the hostnames they answer on - so it is
# declared here. `lan` marks a service with no tunnel, reachable only from the
# office network, so the page can say so rather than offering a dead link.
SERVICES = [
    {"ct": 101, "name": "K2 Plus printer", "url": "https://k2.anthonychiappone.com",
     "desc": "print status, camera, alerts"},
    {"ct": 102, "name": "Dev-Ops", "url": "https://ops.anthonychiappone.com",
     "desc": "this page", "self": True},
    {"ct": 100, "name": "Nginx Proxy Manager", "url": "http://10.20.1.46:81",
     "desc": "unused", "lan": True},
    # The stock Proxmox UI. Not tunnelled, so it is reachable only from the
    # office network - full console, backups and VM management live here.
    {"ct": None, "name": "Proxmox VE", "url": "https://10.20.1.43:8006",
     "desc": "stock PVE interface", "lan": True, "host": True},
    # Not on this host at all - a bookmark, so the page is one place to start
    # from rather than one place plus a browser bookmark bar.
    {"ct": None, "name": "Atlas", "url": "https://atlaspd.com/login",
     "desc": "", "external": True},
]

LOGIN_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dev-Ops</title>
<style>
body{margin:0;background:#0a0f16;color:#fff;font-family:system-ui,sans-serif;
  display:flex;min-height:100vh;align-items:center;justify-content:center}
form{background:#1a1a19;border:1px solid #2b333d;padding:26px 28px;width:300px}
h1{font-size:15px;letter-spacing:.14em;text-transform:uppercase;margin:0 0 18px;color:#c3c2b7}
input{width:100%;box-sizing:border-box;background:#0a0f16;color:#fff;border:1px solid #2b333d;
  padding:9px 10px;margin-bottom:11px;font-size:14px}
button{width:100%;background:#3987e5;color:#fff;border:0;padding:10px;font-size:14px;cursor:pointer}
p{color:#e07a72;font-size:13px;min-height:18px;margin:10px 0 0}
</style></head><body>
<form id="f"><h1>Dev-Ops</h1>
<input id="u" placeholder="username" autocomplete="username" autofocus>
<input id="p" type="password" placeholder="password" autocomplete="current-password">
<button>Sign in</button><p id="e"></p></form>
<script>
document.getElementById("f").onsubmit = async ev => {
  ev.preventDefault();
  const r = await fetch("/api/login", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({u:document.getElementById("u").value,
                          p:document.getElementById("p").value})});
  if (r.ok) location.reload();
  else document.getElementById("e").textContent = "wrong username or password";
};
</script></body></html>"""

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Dev-Ops</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;450&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0f16;--surface-1:#1a1a19;--rule:#2b333d;--rule-2:#20262e;
  --text-primary:#fff;--text-secondary:#c3c2b7;--text-muted:#8a94a0;
  --good:#4fbf8b;--warn:#e0a33c;--crit:#e07a72;--accent:#3987e5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text-primary);
  font-family:"IBM Plex Sans",system-ui,sans-serif;font-size:14px;line-height:1.5}
.wrap{max-width:1180px;margin:0 auto;padding:20px 18px 60px}
header{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
  border-bottom:2px solid var(--text-primary);padding-bottom:12px;margin-bottom:20px}
h1{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:24px;margin:0}
.sub{font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--text-muted)}
.pill{margin-left:auto;font-family:"IBM Plex Mono",monospace;font-size:12px;
  border:1px solid var(--rule);padding:4px 10px}
h2{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-secondary);
  margin:0 0 14px;font-weight:600}
.card{background:var(--surface-1);border:1px solid var(--rule);padding:16px 18px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:1px;background:var(--rule);border:1px solid var(--rule)}
.tile{background:var(--surface-1);padding:10px 12px}
.k{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-muted);margin:0}
.v{font-family:"IBM Plex Mono",monospace;font-size:17px;margin:4px 0 0;
  font-variant-numeric:tabular-nums}
.row{display:flex;justify-content:space-between;gap:12px;align-items:center;
  padding:7px 0;border-bottom:1px solid var(--rule-2)}
.row:last-child{border-bottom:0}
.mono{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.bar{height:6px;background:var(--rule-2);margin-top:5px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--accent)}
.bar i.warn{background:var(--warn)} .bar i.crit{background:var(--crit)}
.good{color:var(--good)} .warn{color:var(--warn)} .crit{color:var(--crit)}
.muted{color:var(--text-muted)}
.note{font-size:12px;color:var(--text-muted);margin:-6px 0 12px}
.svc{display:grid;
  grid-template-columns:minmax(140px,1.1fr) minmax(0,1fr) 150px 120px;
  gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid var(--rule-2)}
@media(max-width:760px){.svc{grid-template-columns:1fr auto;row-gap:4px}}
.svc:last-child{border-bottom:0}
.svc a{color:var(--accent);text-decoration:none;font-size:15px}
.svc a:hover{text-decoration:underline}
.svc .d{color:var(--text-muted);font-size:12px}
.svc .s{text-align:left;font-family:"IBM Plex Mono",monospace;font-size:12px}
button.act{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:11px;
  background:var(--surface-1);color:var(--text-primary);border:1px solid var(--rule);
  padding:3px 9px;cursor:pointer;white-space:nowrap}
button.act:hover:not(:disabled){border-color:var(--accent)}
button.act:disabled{opacity:.4;cursor:not-allowed}
button.act.warn{border-color:var(--warn);color:var(--warn)}
button.act.danger{border-color:var(--crit);color:var(--crit)}
button.act.danger:hover:not(:disabled){background:var(--crit);color:#fff}
#actmsg{font-size:12px;margin:10px 0 0;min-height:17px}
.tag{font-size:10px;letter-spacing:.06em;text-transform:uppercase;border:1px solid var(--rule);
  padding:1px 6px;color:var(--text-muted)}
.err{background:#2a1618;border:1px solid var(--crit);padding:10px 12px;margin-bottom:16px;
  font-size:13px;color:var(--crit)}
</style></head><body>
<div class="wrap">
<header>
  <h1>Dev-Ops</h1><span class="sub" id="host">—</span>
  <span class="pill" id="clock">—</span>
</header>
<div id="errs"></div>

<div class="card">
  <h2>Host</h2>
  <div class="tiles" id="hosttiles"></div>
  <div id="hostact" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center"></div>
  <p id="actmsg" class="muted"></p>
</div>

<div class="card">
  <h2>Services</h2>
  <div id="svcs"></div>
</div>

<div class="grid">
  <div class="card">
    <h2>Consumption</h2>
    <div id="usage"></div>
  </div>
  <div class="card">
    <h2>System</h2>
    <div id="sysinfo"></div>
  </div>
  <div class="card">
    <h2>Power &amp; battery</h2>
    <p class="note">Charger watts are the negotiated USB-C PD contract, not measured draw
    &mdash; this battery reports no power_now.</p>
    <div id="power"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Storage &mdash; per volume</h2>
    <p class="note">Thin allocation, not filesystem use. These diverge: a volume can sit
    at 98% allocated while its filesystem is 9% full, and that is what wedges a container.</p>
    <div id="vols"></div>
  </div>
  <div class="card">
    <h2>Containers</h2>
    <div id="cts"></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Thermals &amp; fans</h2>
    <div id="thermal"></div>
  </div>
  <div class="card">
    <h2>Network</h2>
    <p class="note">vmbr0 must list a physical port. If the USB dongle re-enumerates it
    comes back outside the bridge and the wired path dies silently.</p>
    <div id="net"></div>
  </div>
</div>
</div>
<script>
const el = id => document.getElementById(id);
const gib = b => (b / 1073741824).toFixed(1);
const pctClass = p => p >= 90 ? "crit" : p >= 75 ? "warn" : "";

function bar(pct){
  const c = pctClass(pct);
  return `<div class="bar"><i class="${c}" style="width:${Math.min(100, pct)}%"></i></div>`;
}

const actmsg = (t, cls) => {
  const e = el("actmsg"); e.textContent = t || ""; e.className = cls || "muted";
};
async function doAction(body, label, btn){
  if(btn){ btn.disabled = true; btn.dataset.t = btn.textContent; btn.textContent = "working..."; }
  actmsg(label + "...");
  try{
    const r = await fetch("/api/action", {method:"POST",
      headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    const j = await r.json().catch(()=>({}));
    if(r.status === 401){ location.reload(); return; }
    if(!r.ok || j.ok === false) throw new Error(j.err || j.error || ("HTTP " + r.status));
    actmsg(label + " - " + (j.out || "done"), "good");
    tick();
  }catch(e){
    actmsg(label + " failed: " + (e.message || e), "crit");
  }finally{
    if(btn){ btn.disabled = false; btn.textContent = btn.dataset.t; }
  }
}

async function tick(){
  let d;
  try{
    const r = await fetch("/api/snapshot");
    if(r.status === 401){ location.reload(); return; }
    d = await r.json();
  }catch(e){ el("clock").textContent = "unreachable"; return; }

  el("clock").textContent = new Date().toLocaleTimeString();
  el("errs").innerHTML = (d.errors || []).map(e => `<div class="err">${e}</div>`).join("");

  const n = d.node || {}, h = d.host || {};
  el("host").textContent = (n.pveversion || "") + "  ·  " + (n.kversion || "").slice(0, 34);

  const mem = n.memory || {}, up = n.uptime || 0;
  const cpuPct = (n.cpu || 0) * 100;
  el("hosttiles").innerHTML = [
    ["CPU", cpuPct.toFixed(1) + "%"],
    ["Load", (n.loadavg || []).join("  ")],
    ["Memory", `${gib(mem.used || 0)} / ${gib(mem.total || 0)} GiB`],
    ["Uptime", `${Math.floor(up / 86400)}d ${Math.floor(up % 86400 / 3600)}h`],
    ["Governor", (h.cpu || {}).governor || "—"],
    ["Root FS", n.rootfs ? `${(100 * n.rootfs.used / n.rootfs.total).toFixed(0)}%` : "—"],
  ].map(([k, v]) => `<div class="tile"><p class="k">${k}</p><p class="v">${v}</p></div>`).join("");

  // Host-level actions. The governor toggle offers whichever mode is not
  // current, so it reads as a switch rather than two buttons where one is a
  // no-op. Rejoin only appears when vmbr0 has actually lost its physical port -
  // a button that is almost never needed is better hidden than greyed out.
  const gov = (h.cpu || {}).governor || "";
  const other = gov === "performance" ? "powersave" : "performance";
  const vmbr0Ports = ((h.network || {}).bridges || {})["vmbr0"] || [];
  const vmbr0Phys = vmbr0Ports.filter(x => !x.startsWith("veth"));
  let acts = "";
  if(gov){
    acts += `<button class="act" id="b-gov">Governor: ${gov} &rarr; ${other}</button>`;
  }
  if(vmbr0Phys.length === 0){
    acts += `<button class="act warn" id="b-nic">vmbr0 has no uplink &mdash; rejoin nic0</button>`;
  }
  acts += `<button class="act danger" id="b-hostreboot">Reboot pve-code1</button>`;
  el("hostact").innerHTML = acts;
  if(el("b-gov")) el("b-gov").onclick = ev =>
    doAction({do:"governor", value: other}, `governor -> ${other}`, ev.target);
  if(el("b-nic")) el("b-nic").onclick = ev =>
    doAction({do:"nic_rejoin"}, "rejoining nic0 to vmbr0", ev.target);
  if(el("b-hostreboot")) el("b-hostreboot").onclick = ev => {
    // Two gates on purpose. This is the one action that takes down the page
    // you are clicking from, so the second dialog names what actually stops:
    // both dashboards, both tunnels, and remote access itself.
    const cts = (d.lxc || []).filter(c => c.status === "running").length;
    if(!confirm(`Reboot the Proxmox host pve-code1?\n\n` +
                `This stops ${cts} running containers.`)) return;
    if(!confirm(`Second confirmation.\n\n` +
                `Both dashboards and both Cloudflare tunnels go down.\n` +
                `You will lose remote access for roughly 2 minutes, and if the ` +
                `host does not come back you will need physical access to it.\n\n` +
                `Reboot now?`)) return;
    doAction({do:"host_reboot", confirm:"REBOOT-PVE-CODE1"},
             "rebooting host (1 minute delay)", ev.target);
  };

  // Live consumption. Memory and root filesystem come from the node status;
  // the thin pool comes from the storage list. They measure different things
  // and all three matter: memory is the workload, root is the OS, and the pool
  // is what the containers actually draw from.
  const sw = n.swap || {}, rf = n.rootfs || {};
  const lvm = (d.storage || []).find(x => x.storage === "local-lvm") || {};
  const usage = [];
  const push = (label, used, total, extra) => {
    if(!total) return;
    const pct = 100 * used / total;
    usage.push(`<div class="row" style="display:block">
      <div style="display:flex;justify-content:space-between">
        <span>${label}</span>
        <span class="mono ${pctClass(pct)}">${gib(used)} / ${gib(total)} GiB &nbsp;${pct.toFixed(1)}%${
          extra ? ` <span class="muted">${extra}</span>` : ""}</span>
      </div>${bar(pct)}</div>`);
  };
  push("Memory", mem.used || 0, mem.total || 0);
  push("Swap", sw.used || 0, sw.total || 0);
  push("Root filesystem", rf.used || 0, rf.total || 0);
  push("Thin pool (local-lvm)", lvm.used || 0, lvm.total || 0);
  const disk0 = (d.disks || [])[0];
  if(disk0) usage.push(`<div class="row"><span>Physical disk</span>
    <span class="mono">${(disk0.size / 1e9).toFixed(0)} GB</span></div>`);
  el("usage").innerHTML = usage.join("");

  // Static inventory. Refreshed with everything else, but it does not change -
  // it is here so the box can be identified without opening a shell.
  const ci = n.cpuinfo || {};
  const rows = [
    ["CPU", ci.model || "—"],
    ["Cores / threads", ci.cores != null ? `${ci.cores} cores · ${ci.cpus} threads · ${ci.sockets} socket` : "—"],
    ["Disk", disk0 ? `${disk0.model} (${disk0.type})` : "—"],
    ["Disk health", disk0 ? `${disk0.health}${disk0.wearout != null ? ` · ${disk0.wearout}% life left` : ""}` : "—"],
    ["Proxmox", n.pveversion || "—"],
    ["Kernel", n.kversion ? n.kversion.replace("Linux ", "").slice(0, 46) : "—"],
  ];
  el("sysinfo").innerHTML = rows.map(([k, v]) => {
    const cls = k === "Disk health" && /PASSED/i.test(v) ? "good"
              : k === "Disk health" ? "crit" : "";
    return `<div class="row"><span>${k}</span><span class="mono ${cls}" style="text-align:right">${v}</span></div>`;
  }).join("");

  const pw = h.power || {}, bat = pw.battery || {}, chg = pw.charger || {};
  const prow = (k, v, cls) =>
    `<div class="row"><span>${k}</span><span class="mono ${cls || ""}">${v}</span></div>`;
  let prows = "";
  if(bat.percent != null){
    const st = bat.status || "";
    const cls = st === "Discharging" ? (bat.percent < 20 ? "crit" : "warn") : "good";
    prows += prow("Battery", `${bat.percent}%  ${st}`, cls);
    prows += `<div class="row" style="display:block">${bar(bat.percent)}</div>`;
  }
  prows += prow("On mains", pw.ac_online ? "yes" : "NO - running on battery",
                pw.ac_online ? "good" : "crit");
  if(chg.watts != null)
    prows += prow("Charger", `${chg.volts} V x ${chg.amps} A = ${chg.watts} W`);
  if(pw.minutes_to_full != null)
    prows += prow("Time to full", `${Math.floor(pw.minutes_to_full/60)}h ${
      String(pw.minutes_to_full%60).padStart(2,"0")}m`);
  else if(bat.status === "Full")
    prows += prow("Time to full", "charged");
  if(bat.wh_full != null)
    prows += prow("Capacity", `${bat.wh_now} / ${bat.wh_full} Wh`);
  if(bat.cycles != null) prows += prow("Cycles", bat.cycles);
  el("power").innerHTML = prows;

  // Per-volume thin allocation - the headline metric for this box.
  const vols = (h.volumes || []).filter(v => v.alloc_pct != null);
  el("vols").innerHTML = vols.map(v => `
    <div class="row" style="display:block">
      <div style="display:flex;justify-content:space-between">
        <span>${v.lv}</span>
        <span class="mono ${pctClass(v.alloc_pct)}">${v.alloc_pct.toFixed(1)}% of ${v.size_g.toFixed(0)}G</span>
      </div>${bar(v.alloc_pct)}
    </div>`).join("") || '<p class="muted">no data</p>';
  // fstrim per container volume. This is the fix for the failure that wedged
  // 101 twice: allocation climbing while the filesystem stays nearly empty.
  for(const v of vols){
    const m = /^vm-(\d+)-disk/.exec(v.lv);
    if(!m) continue;
    const row = [...el("vols").children].find(c => c.textContent.includes(v.lv));
    if(!row) continue;
    const b = document.createElement("button");
    b.className = "act"; b.textContent = "fstrim";
    b.style.marginTop = "6px";
    b.onclick = ev => doAction({do:"fstrim", ct: m[1]}, `fstrim ${v.lv}`, ev.target);
    row.appendChild(b);
  }

  // Link each service to its container's live state, so a dead container shows
  // up here rather than as a link that simply fails when clicked.
  const byId = Object.fromEntries((d.lxc || []).map(c => [String(c.vmid), c]));
  el("svcs").innerHTML = (d.services || []).map(sv => {
    const ct = sv.ct == null ? null : byId[String(sv.ct)];
    const up = (sv.host || sv.external) ? true : (ct && ct.status === "running");
    return `<div class="svc">
      <a href="${sv.url}" ${sv.self ? "" : 'target="_blank" rel="noopener"'}>${sv.name}</a>
      <span class="d">${sv.desc || ""}${
        sv.lan ? ' <span class="tag">lan only</span>' : ""}${
        sv.external ? ' <span class="tag">external</span>' : ""}${
        sv.self ? ' <span class="tag">you are here</span>' : ""}</span>
      <span class="s">${(() => {
        const pr = (d.probes || {})[sv.url];
        if(!pr) return '<span class="muted">checking</span>';
        return pr.up
          ? `<span class="good">up</span> <span class="muted">${pr.code} · ${pr.ms}ms</span>`
          : `<span class="crit">DOWN</span> <span class="muted">${pr.err || ""}</span>`;
      })()}</span>
      <span class="s ${up ? "good" : "crit"}">${
        sv.external ? "" : sv.host ? "host" : (ct ? ct.status : "no container")}
        <span class="muted">${sv.external ? "offsite"
          : "· " + (sv.host ? "pve-code1" : sv.ct)}</span></span>
    </div>`;
  }).join("");

  el("cts").innerHTML = (d.lxc || []).map(c => `
    <div class="row"><span>${c.vmid} &nbsp;${c.name}</span>
      <span class="mono ${c.status === "running" ? "good" : "warn"}">${c.status}
        &nbsp;<span class="muted">${Math.round((c.mem || 0) / 1048576)}M</span></span></div>`
  ).join("") || '<p class="muted">none</p>';
  // Restart per container. 102 is excluded: rebooting the container serving
  // this page would kill the request mid-flight and look like a failure.
  for(const c of (d.lxc || [])){
    if(String(c.vmid) === "102") continue;
    const row = [...el("cts").children].find(x => x.textContent.includes(String(c.vmid)));
    if(!row) continue;
    const b = document.createElement("button");
    b.className = "act"; b.style.marginLeft = "10px";
    b.textContent = c.status === "running" ? "restart" : "start";
    b.onclick = ev => {
      const act = c.status === "running" ? "ct_reboot" : "ct_start";
      if(c.status === "running" && !confirm(`Restart container ${c.vmid} (${c.name})?`)) return;
      doAction({do: act, ct: String(c.vmid)}, `${act} ${c.vmid}`, ev.target);
    };
    row.appendChild(b);
  }

  const fans = (h.hwmon || {}).fans || [], pwm = (h.hwmon || {}).pwm || [];
  const temps = (h.hwmon || {}).temps || {};
  const pick = [];
  for(const [chip, list] of Object.entries(temps)){
    for(const t of list){
      if(/Package|Composite|acpitz|temp1/i.test(t.label) || chip === "coretemp")
        pick.push([`${chip} ${t.label}`, t.c]);
    }
  }
  el("thermal").innerHTML =
    fans.map(f => `<div class="row"><span>${f.id.replace("_input","")}</span>
      <span class="mono">${f.rpm} rpm</span></div>`).join("") +
    pwm.map(p => `<div class="row"><span class="muted">${p.id} duty</span>
      <span class="mono muted">${p.value}/255 ${p.enable === "1" ? "(manual)" : ""}</span></div>`).join("") +
    pick.slice(0, 8).map(([k, v]) => `<div class="row"><span>${k}</span>
      <span class="mono ${v >= 85 ? "crit" : v >= 70 ? "warn" : ""}">${v.toFixed(0)} °C</span></div>`).join("");

  const net = h.network || {};
  const brs = Object.entries(net.bridges || {}).map(([b, ports]) => {
    const phys = ports.filter(p => !p.startsWith("veth"));
    const bad = b === "vmbr0" && phys.length === 0;
    return `<div class="row"><span>${b}</span>
      <span class="mono ${bad ? "crit" : phys.length ? "good" : "muted"}">${
        phys.length ? phys.join(", ") : "no physical port"}</span></div>`;
  }).join("");
  const links = Object.entries(net.links || {})
    .filter(([n]) => !n.startsWith("veth"))
    .map(([n, s]) => `<div class="row"><span class="muted">${n}</span>
      <span class="mono ${s.oper === "up" ? "good" : "muted"}">${s.oper}</span></div>`).join("");
  el("net").innerHTML = brs + links;
}
tick();
setInterval(tick, 5000);
</script></body></html>"""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _session_ok(self):
        if not LOGIN_REQUIRED:
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "ops" and v in SESSIONS:
                return True
        return False

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._session_ok():
            if self.path.startswith("/api/"):
                self._json(401, {"error": "not signed in"})
            else:
                self._html(LOGIN_HTML)
            return
        if self.path in ("/", "/index.html"):
            return self._html(PAGE)
        if self.path == "/api/snapshot":
            return self._json(200, snapshot())
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/action":
            if not self._session_ok():
                return self._json(401, {"error": "not signed in"})
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                body = {}
            code, res = act(body)
            return self._json(code, res)
        if self.path == "/api/login":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                b = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                b = {}
            ok = (LOGIN_REQUIRED
                  and secrets.compare_digest(str(b.get("u", "")), AUTH_USER)
                  and secrets.compare_digest(str(b.get("p", "")), AUTH_PASS))
            if not ok:
                return self._json(401, {"error": "bad credentials"})
            sid = secrets.token_urlsafe(32)
            SESSIONS.add(sid)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            self.send_header("Set-Cookie",
                             f"ops={sid}; HttpOnly; SameSite=Strict; Path=/{secure}")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


if __name__ == "__main__":
    class S(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True
    threading.Thread(target=probe_loop, daemon=True).start()
    with S(("127.0.0.1", PORT), H) as srv:
        print(f"pve-ops on 127.0.0.1:{PORT}  node={PVE_NODE}  login={'on' if LOGIN_REQUIRED else 'OFF'}",
              flush=True)
        srv.serve_forever()
