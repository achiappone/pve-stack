import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { randomBytes, timingSafeEqual } from "node:crypto";
import { config, loginRequired } from "./config.js";
import { pve, hostMetrics, hostAction, probe } from "./pve.js";
import { PAGE, LOGIN_HTML } from "./page.js";
import type {
  ActionBody, ActionName, DiskEntry, HostMetrics, LxcEntry,
  NodeStatus, ProbeResult, ServiceEntry, Snapshot, StorageEntry,
} from "./types.js";

/** Which container serves what. Not derivable from the PVE API - it knows the
 *  containers exist but nothing about the hostnames they answer on. */
const SERVICES: ServiceEntry[] = [
  { ct: 101, name: "K2 Plus printer", url: "https://k2.anthonychiappone.com",
    desc: "print status, camera, alerts" },
  { ct: 102, name: "Dev-Ops", url: "https://ops.anthonychiappone.com",
    desc: "this page", self: true },
  { ct: 100, name: "Nginx Proxy Manager", url: "http://10.20.1.46:81",
    desc: "unused", lan: true },
  { ct: null, name: "Proxmox VE", url: "https://10.20.1.43:8006",
    desc: "stock PVE interface", lan: true, host: true },
  { ct: null, name: "Atlas", url: "https://atlaspd.com/login", desc: "", external: true },
];

/** Mirrored from the exporter's allowlist. The exporter is the security
 *  boundary; rejecting here too means a typo cannot even reach it. */
const ALLOWED_ACTIONS: ReadonlySet<ActionName> = new Set<ActionName>([
  "fstrim", "ct_reboot", "ct_start", "ct_stop",
  "nic_rejoin", "governor", "host_reboot", "deploy",
]);

const sessions = new Set<string>();
const probes: Record<string, ProbeResult> = {};

/** Constant-time compare that does not leak length via an early return. */
function safeEqual(a: string, b: string): boolean {
  const ab = Buffer.from(a, "utf8");
  const bb = Buffer.from(b, "utf8");
  if (ab.length !== bb.length) {
    timingSafeEqual(ab, ab);
    return false;
  }
  return timingSafeEqual(ab, bb);
}

function sessionOk(req: IncomingMessage): boolean {
  if (!loginRequired) return true;
  for (const part of (req.headers.cookie ?? "").split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === "ops" && sessions.has(rest.join("="))) return true;
  }
  return false;
}

async function snapshot(): Promise<Snapshot> {
  const out: Snapshot = { ok: true, errors: [], services: SERVICES, probes };
  const n = config.pveNode;
  const jobs: Array<[string, () => Promise<void>]> = [
    ["node status", async () => { out.node = await pve<NodeStatus>(`/nodes/${n}/status`); }],
    ["storage", async () => { out.storage = await pve<StorageEntry[]>(`/nodes/${n}/storage`); }],
    ["disks", async () => { out.disks = await pve<DiskEntry[]>(`/nodes/${n}/disks/list`); }],
    ["lxc", async () => {
      const l = await pve<LxcEntry[]>(`/nodes/${n}/lxc`);
      out.lxc = l.sort((a, b) => a.vmid - b.vmid);
    }],
    ["exporter", async () => { out.host = await hostMetrics(); }],
  ];
  // In parallel: one slow call should not delay the rest, and a failing one
  // must not blank the whole page - each records its own error.
  await Promise.all(jobs.map(async ([label, fn]) => {
    try { await fn(); } catch (e) { out.errors.push(`${label}: ${(e as Error).message}`); }
  }));
  return out;
}

async function probeLoop(): Promise<void> {
  for (;;) {
    await Promise.all(SERVICES.map(async (s) => {
      if (s.url) probes[s.url] = await probe(s.url);
    }));
    await new Promise((r) => setTimeout(r, config.probeSeconds * 1000));
  }
}

function sendJson(res: ServerResponse, code: number, obj: unknown): void {
  const body = Buffer.from(JSON.stringify(obj), "utf8");
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": body.length });
  res.end(body);
}

function sendHtml(res: ServerResponse, html: string): void {
  const body = Buffer.from(html, "utf8");
  res.writeHead(200, {
    "Content-Type": "text/html; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
  });
  res.end(body);
}

async function readBody(req: IncomingMessage, limit = 8192): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > limit) throw new Error("body too large");
    chunks.push(c as Buffer);
  }
  if (!chunks.length) return {};
  try { return JSON.parse(Buffer.concat(chunks).toString("utf8")); } catch { return {}; }
}

const server = createServer((req, res) => {
  void handle(req, res).catch((e: Error) => {
    try { sendJson(res, 500, { error: e.message }); } catch { /* already sent */ }
  });
});

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = req.url ?? "/";

  if (req.method === "POST" && url === "/api/login") {
    const b = (await readBody(req)) as { u?: string; p?: string };
    const ok = loginRequired
      && safeEqual(String(b.u ?? ""), config.user)
      && safeEqual(String(b.p ?? ""), config.pass);
    if (!ok) return sendJson(res, 401, { error: "bad credentials" });
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
    if (url.startsWith("/api/")) return sendJson(res, 401, { error: "not signed in" });
    return sendHtml(res, LOGIN_HTML);
  }

  if (req.method === "POST" && url === "/api/action") {
    const body = (await readBody(req)) as ActionBody;
    if (!ALLOWED_ACTIONS.has(body.do as ActionName)) {
      return sendJson(res, 400, { ok: false, err: `action ${String(body.do)} not allowed` });
    }
    const { status, result } = await hostAction(body);
    return sendJson(res, status, result);
  }

  if (req.method === "GET" && (url === "/" || url === "/index.html")) return sendHtml(res, PAGE);
  if (req.method === "GET" && url === "/api/snapshot") return sendJson(res, 200, await snapshot());

  res.writeHead(404, { "Content-Type": "text/plain" });
  res.end("not found");
}

void probeLoop();
server.listen(config.port, config.bind, () => {
  console.log(
    `pve-ops (typescript) on ${config.bind}:${config.port}  node=${config.pveNode}  ` +
    `login=${loginRequired ? "on" : "OFF"}`,
  );
});
