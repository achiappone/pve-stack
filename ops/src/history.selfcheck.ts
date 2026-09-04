/** Self-check for the parts of history.ts that are actual logic: the outage
 *  state machine and the thinning/retention of the sample files.
 *
 *  Run it:  npx tsx ops/src/history.selfcheck.ts
 *
 *  No framework on purpose - this project has no test dependency and does not
 *  need one to assert five things. It writes only to a temp directory, chosen
 *  before config.ts is imported, so it never touches /var/lib/pve-ops.
 */
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "pve-ops-selfcheck-"));
process.env["OPS_STATE_DIR"] = dir;
process.env["OPS_HISTORY_DAYS"] = "7";

// Dynamic, so the env above is in place before config.ts reads it.
const { observe, observationsFrom, clearDowns, getDowns, thin, readHistory, prune, sampleFrom } =
  await import("./history.js");

const T = 1_757_000_000;                       // a fixed epoch; nothing here is clock-dependent
const day = (t: number) => new Date(t * 1000).toISOString().slice(0, 10);

/* 1. A link that drops and comes back counts exactly one down. */
observe([{ key: "net:eth0", up: true }], T);
assert.equal(getDowns()["net:eth0"]!.downs, 0, "first sighting must not count");
observe([{ key: "net:eth0", up: false }], T + 30);
observe([{ key: "net:eth0", up: true }], T + 60);
{
  const r = getDowns()["net:eth0"]!;
  assert.equal(r.downs, 1);
  assert.equal(r.down, false);
  assert.equal(r.lastDown, T + 30);
  assert.equal(r.lastUp, T + 60);
}

/* 2. Steady state counts nothing and reports no change to persist. */
assert.equal(observe([{ key: "net:eth0", up: true }], T + 90), false, "no-op must not dirty state");
assert.equal(getDowns()["net:eth0"]!.downs, 1);

/* 3. A container that bounced between two samples - running at both ends, but
 *    its uptime went backwards. This is the case a plain status diff misses. */
observe([{ key: "ct:101", up: true, uptime: 4000 }], T);
observe([{ key: "ct:101", up: true, uptime: 12 }], T + 30);
assert.equal(getDowns()["ct:101"]!.downs, 1, "uptime regression is a restart");
observe([{ key: "ct:101", up: true, uptime: 42 }], T + 60);
assert.equal(getDowns()["ct:101"]!.downs, 1, "uptime climbing again is not a new restart");

/* 4. Clearing zeroes the tally but must not resurrect something still down. */
observe([{ key: "net:usb0", up: true }], T);
observe([{ key: "net:usb0", up: false }], T + 30);
assert.equal(getDowns()["net:usb0"]!.downs, 1);
assert.equal(clearDowns("net:usb0"), true);
{
  const r = getDowns()["net:usb0"]!;
  assert.equal(r.downs, 0);
  assert.equal(r.down, true, "clearing history must not claim a dead link is up");
  assert.ok(r.since > T, "since moves to the clear time");
}
assert.equal(clearDowns("net:nope"), false, "unknown target is rejected, not created");

/* 5. A bridge counts a down when its last physical port leaves, even though
 *    every link involved still reads up. */
const brUp = { network: { bridges: { vmbr0: ["nic0", "veth101i0"] }, links: {} } };
const brBad = { network: { bridges: { vmbr0: ["veth101i0"] }, links: {} } };
observe(observationsFrom(brUp as never, []), T);
observe(observationsFrom(brBad as never, []), T + 30);
assert.equal(getDowns()["bridge:vmbr0"]!.downs, 1, "bridge losing its uplink is a down");

/* 6. A failed exporter fetch yields no observations - absence of data must
 *    never read as "everything went down at once". */
assert.deepEqual(observationsFrom(undefined, undefined), []);
assert.equal(observationsFrom(undefined, [{ vmid: 101, status: "running" }]).length, 1);

/* 7. Thinning keeps the bucket peak and the final timestamp. */
{
  const all = Array.from({ length: 1000 }, (_, i) => ({
    t: T + i, temps: { pkg: i === 500 ? 99 : 40 }, fans: { fan1: 1000 },
  }));
  const out = thin(all, 240);
  assert.ok(out.length <= 240, `thinned to ${out.length}`);
  assert.equal(out[out.length - 1]!.t, T + 999, "the last sample survives");
  assert.ok(out.some((s) => s.temps["pkg"] === 99), "a spike must not be thinned away");
  assert.equal(thin(all.slice(0, 10), 240).length, 10, "under the cap is untouched");
}

/* 8. Round-trip through the day files, and retention by unlink. */
{
  const line = (t: number) => `${JSON.stringify({ t, temps: { pkg: 50 }, fans: { fan1: 900 } })}\n`;
  writeFileSync(join(dir, `${day(T)}.jsonl`), line(T - 60) + line(T - 30) + "\n" + "{bad json\n");
  writeFileSync(join(dir, `${day(T - 30 * 86400)}.jsonl`), line(T - 30 * 86400));
  const got = readHistory(1, T);
  assert.equal(got.length, 2, "torn and malformed lines are skipped, not fatal");
  assert.equal(got[0]!.t, T - 60);
  assert.equal(readHistory(1, T - 3600).length, 0, "samples after the window are excluded");

  prune(T);
  const left = readdirSync(dir).filter((f) => f.endsWith(".jsonl"));
  assert.deepEqual(left, [`${day(T)}.jsonl`], "only files older than historyDays go");
}

/* 9. sampleFrom uses the same labels and filter the panel rows use. */
{
  const s = sampleFrom({
    hwmon: {
      fans: [{ chip: "it87", id: "fan1_input", rpm: 2300 }],
      pwm: [],
      temps: {
        coretemp: [{ label: "Package id 0", c: 52.34 }],
        nvme: [{ label: "Composite", c: 41 }, { label: "Sensor 2", c: 38 }],
      },
    },
  } as never, T);
  assert.deepEqual(s.fans, { fan1: 2300 }, "fan ids lose the _input suffix, as in the rows");
  assert.equal(s.temps["coretemp Package id 0"], 52.3);
  assert.equal(s.temps["nvme Composite"], 41);
  assert.ok(!("nvme Sensor 2" in s.temps), "unlabelled extras stay filtered out");
}

console.log("history selfcheck: ok");
