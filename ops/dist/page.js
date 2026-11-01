import { readFileSync } from "node:fs";
// Plain HTML files beside dist/, ported from home-devops/dashboard: the two
// boxes run the same page and differ only in its BOX settings block.
const read = (f) => readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
export const PAGE = read("page.html");
export const LOGIN_HTML = read("login.html");
// The show report (2026-11-01), kept at /classic when Midnight replaced it.
export const CLASSIC = read("classic.html");
//# sourceMappingURL=page.js.map