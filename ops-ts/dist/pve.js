import { request as httpsRequest } from "node:https";
import { request as httpRequest } from "node:http";
import { config, exporterAction } from "./config.js";
/** node:https rather than fetch: the node presents its own CA, and disabling
 *  verification for one internal call needs an agent option that global fetch
 *  does not expose without pulling in undici as a dependency. This server has
 *  none, and keeping it that way is worth a few extra lines. */
function jsonRequest(url, opts = {}) {
    const u = new URL(url);
    const isHttps = u.protocol === "https:";
    const fn = isHttps ? httpsRequest : httpRequest;
    const timeoutMs = opts.timeoutMs ?? 15_000;
    return new Promise((resolve, reject) => {
        const req = fn({
            hostname: u.hostname,
            port: u.port || (isHttps ? 443 : 80),
            path: u.pathname + u.search,
            method: opts.method ?? "GET",
            headers: opts.headers ?? {},
            ...(isHttps && opts.insecure ? { rejectUnauthorized: false } : {}),
        }, (res) => {
            const chunks = [];
            res.on("data", (c) => chunks.push(c));
            res.on("end", () => {
                const text = Buffer.concat(chunks).toString("utf8");
                try {
                    resolve({ status: res.statusCode ?? 0, body: JSON.parse(text || "{}") });
                }
                catch {
                    reject(new Error(`non-JSON from ${u.pathname}: ${text.slice(0, 120)}`));
                }
            });
        });
        req.setTimeout(timeoutMs, () => req.destroy(new Error(`timeout after ${timeoutMs}ms`)));
        req.on("error", reject);
        if (opts.body)
            req.write(opts.body);
        req.end();
    });
}
/** One Proxmox API GET, authenticated with the read-only token. */
export async function pve(path) {
    const { status, body } = await jsonRequest(`${config.pveHost}/api2/json${path}`, {
        headers: { Authorization: `PVEAPIToken=${config.tokenId}=${config.tokenSecret}` },
        insecure: true,
    });
    if (status >= 400)
        throw new Error(`PVE ${status} on ${path}`);
    return body.data;
}
export async function hostMetrics() {
    const { body } = await jsonRequest(config.exporter, { timeoutMs: 10_000 });
    return body;
}
/** Forward an action to the host exporter, which owns the real allowlist. */
export async function hostAction(body) {
    const { status, body: result } = await jsonRequest(exporterAction, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        timeoutMs: 300_000,
    });
    return { status, result };
}
/** Reachability, not correctness: any HTTP answer counts as up. A 401 from a
 *  login-gated dashboard means the server responded, which is the question. */
export async function probe(url) {
    const t0 = Date.now();
    try {
        const { status } = await jsonRequest(url, {
            insecure: true,
            timeoutMs: 8_000,
            headers: { "User-Agent": "pve-ops/1.0" },
        }).catch(async (e) => {
            // A non-JSON 200 (an HTML page) is still a live server.
            if (/non-JSON/.test(e.message))
                return { status: 200, body: null };
            throw e;
        });
        return { up: true, code: status, ms: Date.now() - t0 };
    }
    catch (e) {
        return { up: false, err: e.name || "error", ms: Date.now() - t0 };
    }
}
//# sourceMappingURL=pve.js.map