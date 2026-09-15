import { createServer } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { config, loginRequired } from "./config.js";
import { pve, hostMetrics, hostAction, probe } from "./pve.js";
import { PAGE, LOGIN_HTML } from "./page.js";
import { VERSION } from "./version.js";
import { clearDowns, getDowns, readHistory, sampleLoop } from "./history.js";
/** Which container serves what. Not derivable from the PVE API - it knows the
 *  containers exist but nothing about the hostnames they answer on. */
const SERVICES = [
    { ct: 101, name: "K2 Plus printer", url: "https://k2.anthonychiappone.com",
        desc: "print status, camera, alerts" },
    { ct: 102, name: "Dev-Ops", url: "https://ops.anthonychiappone.com",
        desc: "this page", self: true },
    { ct: 100, name: "Nginx Proxy Manager", url: "http://10.20.1.46:81",
        desc: "unused", lan: true },
    { ct: null, name: "Proxmox VE", url: "https://pve.anthonychiappone.com",
        desc: "stock PVE interface", host: true },
    // The lifeline. Worth a row not because you click it, but because this is
    // where you find out it still works - on the day the wired path dies, this
    // is the only way in, and by then it is too late to discover it is broken.
    { ct: null, name: "Host SSH (lifeline)", url: "https://pve-direct.anthonychiappone.com",
        desc: "ssh pve-direct - host tunnel, survives a NIC failure via wifi",
        host: true, ssh: true },
    // Same dashboard, second route. This one is published by the tunnel on the
    // HOST and reaches pve-ops across vmbr1, which has no physical port and so
    // keeps working when nic0 dies. ops.* goes through CT 102's own tunnel and
    // does not - on 2026-09-09 it was unreachable for four hours while the host
    // was fine.
    { ct: 102, name: "Dev-Ops (wifi route)", url: "https://ops-direct.anthonychiappone.com",
        desc: "this page again, via the host tunnel - up when ops.* is not" },
    { ct: 103, name: "Beszel", url: "https://beszel.anthonychiappone.com",
        desc: "lightweight host + container metrics" },
    { ct: 104, name: "Uptime Kuma", url: "https://uptime.anthonychiappone.com",
        desc: "uptime checks and alerting" },
    { ct: 105, name: "Pulse", url: "https://pulse.anthonychiappone.com",
        desc: "Proxmox VE / PBS monitoring" },
    // Not a container - an ESP32 on the DMX rig, reached across the office LAN
    // rather than vmbr1. The probe row is the whole point of listing it: a bad
    // self-test bricked this board on 2026-09-11 and it sat in a panic-reboot
    // loop for four days, because a panic in setup() never reaches WiFi and so
    // cannot be recovered over the air. Nothing was watching.
    { ct: null, name: "Haze regulator", url: "https://haze.anthonychiappone.com",
        desc: "PM2.5-regulated hazer - DMX output, live chart" },
    { ct: null, name: "Atlas", url: "https://atlaspd.com/login", desc: "", external: true },
];
/** Mirrored from the exporter's allowlist. The exporter is the security
 *  boundary; rejecting here too means a typo cannot even reach it.
 *
 *  clear_downs is deliberately absent: it is this server's own state, so it is
 *  answered before this check rather than forwarded to an exporter that has
 *  never heard of it. */
const ALLOWED_ACTIONS = new Set([
    "fstrim", "ct_reboot", "ct_start", "ct_stop",
    "nic_rejoin", "governor", "host_reboot", "deploy", "platform_profile",
]);
const sessions = new Set();
const probes = {};
/** Constant-time compare that does not leak length via an early return. */
function safeEqual(a, b) {
    const ab = Buffer.from(a, "utf8");
    const bb = Buffer.from(b, "utf8");
    if (ab.length !== bb.length) {
        timingSafeEqual(ab, ab);
        return false;
    }
    return timingSafeEqual(ab, bb);
}
function sessionOk(req) {
    if (!loginRequired)
        return true;
    for (const part of (req.headers.cookie ?? "").split(";")) {
        const [k, ...rest] = part.trim().split("=");
        if (k === "ops" && sessions.has(rest.join("=")))
            return true;
    }
    return false;
}
async function snapshot() {
    // downs is a plain in-memory read, so it belongs here and not in the
    // parallel jobs below - there is nothing to await and nothing to fail.
    const out = { ok: true, errors: [], services: SERVICES, probes,
        downs: getDowns(), version: VERSION };
    const n = config.pveNode;
    const jobs = [
        ["node status", async () => { out.node = await pve(`/nodes/${n}/status`); }],
        ["storage", async () => { out.storage = await pve(`/nodes/${n}/storage`); }],
        ["disks", async () => { out.disks = await pve(`/nodes/${n}/disks/list`); }],
        ["lxc", async () => {
                const l = await pve(`/nodes/${n}/lxc`);
                out.lxc = l.sort((a, b) => a.vmid - b.vmid);
            }],
        ["exporter", async () => { out.host = await hostMetrics(); }],
    ];
    // In parallel: one slow call should not delay the rest, and a failing one
    // must not blank the whole page - each records its own error.
    await Promise.all(jobs.map(async ([label, fn]) => {
        try {
            await fn();
        }
        catch (e) {
            out.errors.push(`${label}: ${e.message}`);
        }
    }));
    return out;
}
async function probeLoop() {
    for (;;) {
        await Promise.all(SERVICES.map(async (s) => {
            if (s.url)
                probes[s.url] = await probe(s.url);
        }));
        await new Promise((r) => setTimeout(r, config.probeSeconds * 1000));
    }
}
function sendJson(res, code, obj) {
    const body = Buffer.from(JSON.stringify(obj), "utf8");
    res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
    res.end(body);
}
function sendHtml(res, html) {
    const body = Buffer.from(html, "utf8");
    res.writeHead(200, {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Length": body.length,
        "Cache-Control": "no-store",
    });
    res.end(body);
}
async function readBody(req, limit = 8192) {
    const chunks = [];
    let size = 0;
    for await (const c of req) {
        size += c.length;
        if (size > limit)
            throw new Error("body too large");
        chunks.push(c);
    }
    if (!chunks.length)
        return {};
    try {
        return JSON.parse(Buffer.concat(chunks).toString("utf8"));
    }
    catch {
        return {};
    }
}
const server = createServer((req, res) => {
    void handle(req, res).catch((e) => {
        try {
            sendJson(res, 500, { error: e.message });
        }
        catch { /* already sent */ }
    });
});
async function handle(req, res) {
    const url = req.url ?? "/";
    if (req.method === "POST" && url === "/api/login") {
        const b = (await readBody(req));
        const ok = loginRequired
            && safeEqual(String(b.u ?? ""), config.user)
            && safeEqual(String(b.p ?? ""), config.pass);
        if (!ok)
            return sendJson(res, 401, { error: "bad credentials" });
        const sid = randomBytes(32).toString("base64url");
        sessions.add(sid);
        // SameSite=Strict is the CSRF defence: the browser will not attach this
        // cookie to a request another site initiated.
        const secure = req.headers["x-forwarded-proto"] === "https" ? "; Secure" : "";
        const body = Buffer.from(JSON.stringify({ ok: true }), "utf8");
        res.writeHead(200, {
            "Content-Type": "application/json",
            "Content-Length": body.length,
            "Set-Cookie": `ops=${sid}; HttpOnly; SameSite=Strict; Path=/${secure}`,
        });
        return void res.end(body);
    }
    // Gate before any route. Putting it after the "/" handler once meant an
    // unauthenticated page loaded, its JS got 401, reloaded, and looped.
    if (!sessionOk(req)) {
        if (url.startsWith("/api/"))
            return sendJson(res, 401, { error: "not signed in" });
        return sendHtml(res, LOGIN_HTML);
    }
    if (req.method === "POST" && url === "/api/action") {
        const body = (await readBody(req));
        if (body.do === "clear_downs") {
            const ok = clearDowns(String(body.target ?? ""));
            return sendJson(res, ok ? 200 : 400, ok ? { ok: true, out: "cleared" } : { ok: false, err: "unknown target" });
        }
        if (!ALLOWED_ACTIONS.has(body.do)) {
            return sendJson(res, 400, { ok: false, err: `action ${String(body.do)} not allowed` });
        }
        const { status, result } = await hostAction(body);
        return sendJson(res, status, result);
    }
    if (req.method === "GET" && (url === "/" || url === "/index.html")) {
        return sendHtml(res, PAGE.replace("{{VERSION}}", VERSION));
    }
    if (req.method === "GET" && url === "/api/snapshot")
        return sendJson(res, 200, await snapshot());
    if (req.method === "GET" && url.startsWith("/api/history")) {
        // Clamped, not trusted: this number sizes a read loop, and NaN would make
        // the cutoff NaN and quietly return nothing.
        const raw = Number(new URL(url, "http://x").searchParams.get("hours"));
        // Floor is 0.25h, not 1h: the Range dropdown offers 30 minutes, and a
        // minimum of 1 silently widened it back to an hour.
        const hours = Number.isFinite(raw)
            ? Math.min(Math.max(raw, 0.25), config.historyDays * 24)
            : 6;
        return sendJson(res, 200, readHistory(hours));
    }
    res.writeHead(404, { "Content-Type": "text/plain" });
    res.end("not found");
}
void probeLoop();
void sampleLoop();
server.listen(config.port, config.bind, () => {
    console.log(`pve-ops (typescript) on ${config.bind}:${config.port}  node=${config.pveNode}  ` +
        `login=${loginRequired ? "on" : "OFF"}`);
});
//# sourceMappingURL=server.js.map