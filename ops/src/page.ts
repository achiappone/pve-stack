import { readFileSync } from "node:fs";

// Plain HTML files beside dist/, ported from home-devops/dashboard: the two
// boxes run the same page and differ only in its BOX settings block.
const read = (f: string) => readFileSync(new URL(`../${f}`, import.meta.url), "utf8");
export const PAGE = read("page.html");
export const LOGIN_HTML = read("login.html");
