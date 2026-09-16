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
            // Content-Length is mandatory here, not optional. Without it node uses
            // chunked transfer-encoding, and the exporter reads exactly
            // Content-Length bytes - so it saw an empty body and every action came
            // back as "unknown action ''".
            headers: {
                ...(opts.headers ?? {}),
                ...(opts.body ? { "Content-Length": String(Buffer.byteLength(opts.body)) } : {}),
            },
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
/** What the printer is doing, in the two objects that answer it.
 *
 *  Deliberately not the whole status payload: this is one row on a page about
 *  containers, and the K2 dashboard is one click away for anything more. */
export async function printerSummary() {
    const { body } = await jsonRequest(`${config.printerUrl}/printer/objects/query?print_stats&virtual_sdcard`, { timeoutMs: 6_000 });
    const st = body?.result?.status ?? {};
    const ps = st.print_stats ?? {};
    const progress = st.virtual_sdcard?.progress;
    const percent = typeof progress === "number" ? progress * 100 : null;
    // Linear from elapsed. The floor is 5%, not the 0.5% the K2 dashboard uses,
    // because print_duration counts heating and priming: measured at 1.2% into a
    // file named 3h32m, the same formula claimed 10h 24m left. On a one-line
    // summary with no chart beside it there is nothing to contradict a number
    // like that, so it is better withheld than wrong.
    const elapsed = ps.print_duration ?? 0;
    const remainingSeconds = percent !== null && percent >= 5 ? (elapsed * (100 - percent)) / percent : null;
    return {
        state: ps.state ?? "unknown",
        percent,
        filename: ps.filename || undefined,
        remainingSeconds,
    };
}
/** What the haze regulator is doing. Same idea as printerSummary: the row
 *  says whether it is hazing and at what, and its own page has the rest. */
export async function hazeSummary() {
    const auth = Buffer.from(`${config.hazeUser}:${config.hazePass}`).toString("base64");
    const { status, body } = await jsonRequest(`${config.hazeUrl}/api/state`, {
        headers: { Authorization: `Basic ${auth}` },
        timeoutMs: 6_000,
    });
    if (status >= 400)
        throw new Error(`haze ${status}`);
    return {
        pm25: body.pm25 ?? 0,
        output: body.output ?? 0,
        automatic: body.automatic === true,
        stopped: body.stopped === true,
        sensorOk: body.sensorOk !== false,
    };
}
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