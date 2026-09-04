# pve-stack

Everything running on **pve-code1**, a Proxmox VE 9.2 host on a Dell XPS 15 9570.

Until now these files existed only on the box itself. Three of them
(`opsdash.py`, `pve-exporter.py`, `camrelay.py`) had no copy anywhere else, on a
laptop that has already powered itself off unexpectedly once.

## Layout

    ops/                    Dev-Ops dashboard (TS)     container 102, ops.anthonychiappone.com
                            src/ is the source, dist/ is committed - the container
                            needs only a node runtime, there are no runtime deps
    exporter/               host metrics + actions     runs on the HOST, bound to vmbr1
    camera/camrelay.py      WebRTC -> MJPEG relay      container 101
    deploy/                 deploy + maintenance       k2-deploy, k2-set-smtp, fstrim cron
    systemd/                unit files for all three

The K2 printer dashboard itself lives in its own repo,
`github.com/achiappone/k2plus-dashboard`, and is deployed by `deploy/k2-deploy`.

## Why it is shaped this way

**Each container runs its own `cloudflared`.** Not one shared tunnel, and not
Nginx Proxy Manager. `cloudflared` already routes by hostname, so NPM would add
a hop for a job the tunnel does. More importantly, services bind `127.0.0.1`,
so nothing is reachable from the office LAN - a shared reverse proxy would
force them onto `0.0.0.0`. Container 101 wedged twice in one day; separate
tunnels meant the ops dashboard stayed up to show why.

**The exporter runs on the host, not in a container.** Two things the PVE API
cannot answer:

* **per-volume thin allocation.** The API reports the pool. `vm-101-disk-0`
  reached 98% while the pool sat at 1%, and wedged the container twice. The
  filesystem was 9% full the whole time - LVM-thin allocates on write and never
  returns blocks without TRIM, and an unprivileged container cannot issue
  FITRIM. `pct fstrim <id>` from the host does, on a running container.
* **temperatures, fans and battery.** Not in the API at all.

**The exporter listens on `vmbr1` (10.10.10.1)**, a bridge with no physical
port, so it is unreachable from the LAN. It runs as root and can execute a
fixed allowlist of actions, so treat anything attached to `vmbr1` as trusted:
container 102 is currently the only member.

## Hard-won details

* **Chromium cache churn** filled a 32G volume in hours while holding only 2.7G
  of files. `--disk-cache-size=1 --media-cache-size=1` cut allocation growth
  from ~4.2%/min to ~0.002%/min. A nightly `pct fstrim` is the backstop.
* **The USB ethernet dongle re-enumerates** onto a different bus and comes back
  *outside* `vmbr0`, killing the wired path with no error logged anywhere. The
  wifi backup path exists for exactly this. `nic_rejoin` fixes it in one click.
* **The camera needs a real browser.** aiortc gets ICE working after the
  printer's malformed SDP is sanitised, but DTLS never completes - the
  printer's `pear` stack ignores the ClientHello. Headless Chromium negotiates
  fine, so the relay drives one and re-serves MJPEG.
* **Cloudflare replaces origin 5xx** with its own error page, swallowing the
  body. Return 4xx for anything whose message the UI needs to show.

## Versioning

    1.00.001
    │ │  └── patch  small changes, fixes, tweaks: 002, 003, 004...
    │ └───── minor  bigger changes: a new panel, a new control
    └─────── major  architecture changes

Defined once in `ops/src/version.ts`. The server substitutes it into the page
header and returns it on `/api/snapshot`, so you can tell whether what you are
looking at is the code you just deployed without opening a shell.

Bump it in the same commit as the change, and tag the commit `vX.YY.ZZZ`.

Note that `pve-deploy` pulls from GitHub, not from a working copy - a change
that is committed but not pushed will not deploy, and the deploy will report
success having installed the previous version.

## Secrets

None are in this repo. They live in env files at mode 600 on the boxes:
`/etc/pve-ops.env` (PVE API token, dashboard login) and `/etc/k2-dashboard.env`
(control token, SMTP password).
