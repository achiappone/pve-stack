#!/usr/bin/env tsx
/** Task runner for pve-stack development.
 *
 *  Tasks live in dev/tasks.yaml rather than in package.json scripts, because
 *  several of them are multi-line shell and package.json has nowhere sensible
 *  to put that. package.json still exposes one npm script per task so they
 *  appear in WebStorm's npm panel - those just delegate back here. */
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parse } from "yaml";

interface Task { desc?: string; run: string; tty?: boolean }
interface TaskFile { host: string; tasks: Record<string, Task> }

const here = dirname(fileURLToPath(import.meta.url));
const spec = parse(readFileSync(join(here, "tasks.yaml"), "utf8")) as TaskFile;

const name = process.argv[2];

if (!name || name === "--list") {
  const width = Math.max(...Object.keys(spec.tasks).map((k) => k.length));
  console.log(`\npve-stack tasks  (host: ${spec.host})\n`);
  for (const [k, t] of Object.entries(spec.tasks)) {
    console.log(`  ${k.padEnd(width)}  ${t.desc ?? ""}`);
  }
  console.log(`\n  npm run dev <task>\n`);
  process.exit(0);
}

const task = spec.tasks[name];
if (!task) {
  console.error(`unknown task "${name}". Run "npm run dev" to list them.`);
  process.exit(1);
}

// -t for interactive tasks so shells and watch loops get a real terminal;
// without it ctrl-c does not reach the remote side and clear() does nothing.
const args = task.tty ? ["-t", spec.host] : [spec.host];
const res = spawnSync("ssh", [...args, task.run], { stdio: "inherit" });
process.exit(res.status ?? 1);
