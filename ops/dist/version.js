/** Project version.
 *
 *  1.00.001
 *  │ │  └── patch: small changes, fixes, tweaks. 001, 002, 003...
 *  │ └───── minor: bigger changes, new panels, new controls.
 *  └─────── major: architecture changes.
 *
 *  Single source of truth. The server substitutes it into the page and reports
 *  it on /api/snapshot, so "is what I am looking at the code I just deployed?"
 *  has an answer that does not require an ssh session. */
export const VERSION = "1.03.001";
//# sourceMappingURL=version.js.map