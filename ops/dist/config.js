import { readFileSync } from "node:fs";
/** Read a shell-style KEY=VALUE env file.
 *
 *  systemd's EnvironmentFile format: no quoting, no interpolation, no export.
 *  Parsing it directly rather than relying on the unit means the same values
 *  are available when running the server by hand. */
export function readEnvFile(path) {
    const out = {};
    let raw;
    try {
        raw = readFileSync(path, "utf8");
    }
    catch {
        return out;
    }
    for (const line of raw.split("\n")) {
        const t = line.trim();
        if (!t || t.startsWith("#"))
            continue;
        const i = t.indexOf("=");
        if (i < 0)
            continue;
        out[t.slice(0, i)] = t.slice(i + 1);
    }
    return out;
}
const env = readEnvFile(process.env["OPS_ENV_FILE"] ?? "/etc/pve-ops.env");
const pick = (k, d = "") => process.env[k] ?? env[k] ?? d;
export const config = {
    pveHost: pick("PVE_HOST", "https://127.0.0.1:8006"),
    pveNode: pick("PVE_NODE", "pve"),
    tokenId: pick("PVE_TOKEN_ID"),
    tokenSecret: pick("PVE_TOKEN_SECRET"),
    exporter: pick("EXPORTER_URL", "http://10.10.10.1:9101/metrics"),
    port: Number(pick("OPS_PORT", "8780")),
    bind: pick("OPS_BIND", "127.0.0.1"),
    user: pick("OPS_USER"),
    pass: pick("OPS_PASS"),
    probeSeconds: Number(pick("OPS_PROBE_SECONDS", "60")),
    // Sampling for the thermal history and the down counters. Deliberately
    // slower than the page's 5s tick: at 5s this writes 17k lines a day to an
    // SSD whose wearout this same dashboard reports. 30s is ~2900 lines.
    stateDir: pick("OPS_STATE_DIR", "/var/lib/pve-ops"),
    // 10s, not 30s: the Range dropdown's 30 minute view is 180 samples at this
    // rate, which is under MAX_POINTS, so that view is drawn at full resolution
    // with nothing thinned away. Longer ranges still bucket down to 240 points,
    // so the chart costs the same to draw as it always did - only the on-disk
    // history grows, to roughly 60k lines a week.
    historySeconds: Number(pick("OPS_HISTORY_SECONDS", "10")),
    historyDays: Number(pick("OPS_HISTORY_DAYS", "7")),
};
export const loginRequired = Boolean(config.user && config.pass);
export const exporterAction = config.exporter.replace("/metrics", "/action");
//# sourceMappingURL=config.js.map