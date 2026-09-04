/** Retained state: thermal history and outage counters.
 *
 *  This is the first thing in the stack that remembers anything. The exporter
 *  reads sysfs per request, the PVE API is a live view, and snapshot() builds a
 *  fresh object every time - so until now a fan curve or a 3am link flap simply
 *  did not exist anywhere to be looked at.
 *
 *  Two separate concerns share one loop because they want the same two fetches:
 *
 *    - history: one JSON line per sample, appended to a file per UTC day.
 *      Retention is unlink on old files, so there is no compaction, no line
 *      counting and no rewrite-in-place to get wrong.
 *    - downs:   a state machine over link/bridge/container up-ness, counting
 *      transitions. Persisted as one small JSON object, written only when it
 *      actually changes.
 *
 *  The loop runs on its own timer rather than off the page's 5s tick: nothing
 *  polls when the browser tab is closed, and overnight is precisely when the
 *  things this records happen.
 */
import { appendFileSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { config } from "./config.js";
import { pve, hostMetrics } from "./pve.js";
/** Enough points to draw a smooth line on a card-width chart, few enough that
 *  the JSON stays small. */
const MAX_POINTS = 240;
const DAY_FILE = /^(\d{4}-\d{2}-\d{2})\.jsonl$/;
const downsFile = () => join(config.stateDir, "downs.json");
const dayFile = (t) => join(config.stateDir, `${day(t)}.jsonl`);
const day = (t) => new Date(t * 1000).toISOString().slice(0, 10);
const now = () => Math.floor(Date.now() / 1000);
/* ------------------------------------------------------------ down counters */
let downs = {};
/** Last observed up-ness, and for containers their uptime. Purely in-memory:
 *  after a restart the first sample re-seeds it, which at worst misses one
 *  transition that happened while this process was not running. */
const lastSeen = {};
/** The state machine, kept free of I/O so it can be exercised directly.
 *  Returns true if any record changed, which is the signal to persist. */
export function observe(obs, t) {
    let changed = false;
    for (const o of obs) {
        const prev = lastSeen[o.key];
        let rec = downs[o.key];
        if (!rec) {
            // First sighting. Something already down is recorded as down but not
            // counted - we have no idea whether it just fell over or has been off
            // since the box booted, and guessing would inflate the tally.
            rec = { downs: 0, down: !o.up, since: t, ...(o.up ? {} : { lastDown: t }) };
            downs[o.key] = rec;
            changed = true;
        }
        else if (prev && prev.up && !o.up) {
            rec.downs += 1;
            rec.down = true;
            rec.lastDown = t;
            changed = true;
        }
        else if (prev && !prev.up && o.up) {
            rec.down = false;
            rec.lastUp = t;
            changed = true;
        }
        else if (prev && prev.up && o.up
            && prev.uptime !== undefined && o.uptime !== undefined && o.uptime < prev.uptime) {
            // Up at both ends but the clock went backwards: it bounced inside one
            // sample interval. Without this a container that restarts in under 30s
            // is invisible, and the counter quietly under-reports rather than
            // reporting nothing - which is worse, because it looks trustworthy.
            rec.downs += 1;
            rec.down = false;
            rec.lastDown = t;
            rec.lastUp = t;
            changed = true;
        }
        lastSeen[o.key] = o.uptime === undefined ? { up: o.up } : { up: o.up, uptime: o.uptime };
    }
    return changed;
}
export const getDowns = () => downs;
/** Zero one target's tally. The live `down` flag is preserved: clearing the
 *  history of a link that is down right now must not make it look up. */
export function clearDowns(target) {
    const rec = downs[target];
    if (!rec)
        return false;
    rec.downs = 0;
    rec.since = now();
    delete rec.lastDown;
    delete rec.lastUp;
    saveDowns();
    return true;
}
function saveDowns() {
    try {
        mkdirSync(config.stateDir, { recursive: true });
        writeFileSync(downsFile(), JSON.stringify(downs), "utf8");
    }
    catch (e) {
        console.error(`downs: cannot write ${downsFile()}: ${e.message}`);
    }
}
function loadDowns() {
    try {
        downs = JSON.parse(readFileSync(downsFile(), "utf8"));
    }
    catch {
        downs = {}; // absent or corrupt: start a fresh tally
    }
}
/** Drop records for things that no longer exist and never went down: an
 *  interface that was renamed or removed, a container that was deleted, or a
 *  key an older version of this code created and no longer does.
 *
 *  Only ever called when both fetches succeeded, so an empty observation list
 *  means "nothing is there" rather than "we could not ask". Anything with a
 *  tally is kept - that is history someone may still want to look at. */
export function pruneStale(obs) {
    const live = new Set(obs.map((o) => o.key));
    let changed = false;
    for (const key of Object.keys(downs)) {
        if (!live.has(key) && downs[key].downs === 0) {
            delete downs[key];
            delete lastSeen[key];
            changed = true;
        }
    }
    return changed;
}
/** Turn one poll into the flat list the state machine wants. Only things we
 *  actually heard about are included - a failed exporter fetch must not read
 *  as "every link went down at once". */
export function observationsFrom(host, lxc) {
    const obs = [];
    for (const [name, link] of Object.entries(host?.network?.links ?? {})) {
        if (name.startsWith("veth"))
            continue; // container side, churns by design
        obs.push({ key: `net:${name}`, up: link.oper === "up" });
    }
    for (const [br, ports] of Object.entries(host?.network?.bridges ?? {})) {
        // A bridge whose last physical port left is the failure this box actually
        // has: the USB NIC re-enumerates, the bridge stays "up", and the wired
        // path is gone. Link state alone never shows it.
        const up = ports.some((p) => !p.startsWith("veth"));
        // But vmbr1 has no physical port on purpose - that is the whole reason the
        // exporter is only reachable from containers attached to it. Judging every
        // bridge by the same rule marks it permanently down. Only bridges that
        // have an uplink, or already lost one we were watching, are tracked.
        if (up || `bridge:${br}` in downs)
            obs.push({ key: `bridge:${br}`, up });
    }
    for (const c of lxc ?? []) {
        obs.push({ key: `ct:${c.vmid}`, up: c.status === "running", uptime: c.uptime ?? 0 });
    }
    return obs;
}
/* ---------------------------------------------------------------- samples */
/** Pull out the same series the Thermals panel already shows, under the same
 *  labels, so the chart and the rows below it cannot disagree. */
export function sampleFrom(host, t) {
    const s = { t, temps: {}, fans: {} };
    for (const f of host?.hwmon?.fans ?? [])
        s.fans[f.id.replace("_input", "")] = f.rpm;
    for (const [chip, list] of Object.entries(host?.hwmon?.temps ?? {})) {
        for (const r of list) {
            if (/Package|Composite|acpitz|temp1/i.test(r.label) || chip === "coretemp") {
                s.temps[`${chip} ${r.label}`] = Math.round(r.c * 10) / 10;
            }
        }
    }
    return s;
}
function appendSample(s) {
    try {
        mkdirSync(config.stateDir, { recursive: true });
        appendFileSync(dayFile(s.t), `${JSON.stringify(s)}\n`, "utf8");
    }
    catch (e) {
        console.error(`history: cannot append ${dayFile(s.t)}: ${e.message}`);
    }
}
/** Retention: delete whole day files. No compaction, no rewriting. */
export function prune(t = now()) {
    const cutoff = day(t - config.historyDays * 86400);
    let names;
    try {
        names = readdirSync(config.stateDir);
    }
    catch {
        return;
    }
    for (const n of names) {
        const m = DAY_FILE.exec(n);
        if (m && m[1] < cutoff) {
            try {
                unlinkSync(join(config.stateDir, n));
            }
            catch { /* raced or gone */ }
        }
    }
}
/** Read back the last `hours` of samples, thinned to at most MAX_POINTS.
 *
 *  Deliberately uncached. Seven days is ~20k lines and parses in tens of
 *  milliseconds, and this is only called when someone changes the range
 *  dropdown - a cache here would be state to invalidate for no gain. */
export function readHistory(hours, t = now()) {
    const cutoff = t - hours * 3600;
    const from = day(cutoff);
    let names;
    try {
        names = readdirSync(config.stateDir);
    }
    catch {
        return [];
    }
    const days = names
        .map((n) => DAY_FILE.exec(n))
        .filter((m) => m !== null && m[1] >= from)
        .map((m) => m[0])
        .sort(); // ISO dates sort chronologically
    const out = [];
    for (const f of days) {
        let raw;
        try {
            raw = readFileSync(join(config.stateDir, f), "utf8");
        }
        catch {
            continue;
        }
        for (const line of raw.split("\n")) {
            if (!line)
                continue;
            try {
                const s = JSON.parse(line);
                if (s.t >= cutoff && s.t <= t)
                    out.push(s);
            }
            catch { /* a torn last line from a killed process; skip it */ }
        }
    }
    return thin(out, MAX_POINTS);
}
/** Bucket to at most `max` points, keeping the highest value seen in each
 *  bucket per series. A thermal chart is read for its peaks, so decimating by
 *  stride - which would drop a spike that fell between two picks - would make
 *  the quiet chart the untrustworthy one.
 *
 *  ponytail: peaks survive, troughs flatten. Return per-bucket min/max bands
 *  if the smoothed floor ever misleads. */
export function thin(all, max) {
    if (all.length <= max)
        return all;
    const size = Math.ceil(all.length / max);
    const out = [];
    for (let i = 0; i < all.length; i += size) {
        const bucket = all.slice(i, i + size);
        const merged = { t: bucket[bucket.length - 1].t, temps: {}, fans: {} };
        for (const s of bucket) {
            for (const [k, v] of Object.entries(s.temps)) {
                if (!(k in merged.temps) || v > merged.temps[k])
                    merged.temps[k] = v;
            }
            for (const [k, v] of Object.entries(s.fans)) {
                if (!(k in merged.fans) || v > merged.fans[k])
                    merged.fans[k] = v;
            }
        }
        out.push(merged);
    }
    return out;
}
/* ------------------------------------------------------------------- loop */
/** One poll: fetch both sources independently, so the exporter being down
 *  still leaves container tracking working, and vice versa. */
async function sampleOnce() {
    const t = now();
    const [host, lxc] = await Promise.all([
        hostMetrics().catch(() => undefined),
        pve(`/nodes/${config.pveNode}/lxc`).catch(() => undefined),
    ]);
    if (host)
        appendSample(sampleFrom(host, t));
    if (host || lxc) {
        const obs = observationsFrom(host, lxc);
        let changed = observe(obs, t);
        // Only safe to prune when the whole picture came back; a half-failed poll
        // would otherwise read as "these things no longer exist".
        if (host && lxc && pruneStale(obs))
            changed = true;
        if (changed)
            saveDowns();
    }
}
export async function sampleLoop() {
    loadDowns();
    let lastPrune = 0;
    for (;;) {
        try {
            await sampleOnce();
            // Once a day is plenty for deleting day files.
            if (now() - lastPrune > 86400) {
                prune();
                lastPrune = now();
            }
        }
        catch (e) {
            console.error(`history: ${e.message}`);
        }
        await new Promise((r) => setTimeout(r, config.historySeconds * 1000));
    }
}
//# sourceMappingURL=history.js.map