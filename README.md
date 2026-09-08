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
    modprobe.d/             module options             fan control, usb-storage quirks
    udev/                   device rules               backup enclosure autosuspend
    wifi/                   secondary path + failover  wifi-up/down, wifi-failover

Monitoring lives in 103 (beszel), 104 (uptime-kuma) and 105 (pulse). Those are
installed from upstream releases rather than from this repo, but each binds only
its vmbr1 address, so the ops tunnel is the one way in and nothing of theirs is
on the office LAN.

Backups go to a USB disk mounted at `/mnt/backup`, registered as the
`backup-usb` storage with `is_mountpoint 1` - without that flag a run while the
disk is unmounted writes into the empty directory and fills the root
filesystem. The fstab entry is `nofail`: this host has no remote hands, and a
USB disk that fails to appear must never hold up boot.

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
fixed allowlist of actions. Bridge membership used to be the whole access
model, back when 102 was the only member; the monitoring containers sit there
too now, and those are internet-facing, so the exporter checks the peer address
against `EXPORTER_ALLOW` (102 alone) rather than trusting the bridge.

## Hard-won details

* **Chromium cache churn** filled a 32G volume in hours while holding only 2.7G
  of files. `--disk-cache-size=1 --media-cache-size=1` cut allocation growth
  from ~4.2%/min to ~0.002%/min. A nightly `pct fstrim` is the backstop.
* **The USB ethernet dongle re-enumerates** onto a different bus and comes back
  *outside* `vmbr0`, killing the wired path with no error logged anywhere.
  `nic_rejoin` fixes it in one click. The wifi backup path exists for exactly
  this, but until 2026-09-08 it did not actually work: `wifi-up.sh` runs dhcpcd
  with `-G`, so wifi had an address and no way out. That flag is still right -
  wifi must not carry traffic in normal operation - so the failover is active
  instead. `wifi-failover.timer` probes the wired gateway every 30s and swaps
  the default route when it stops answering.

  A route metric would not have done it. `vmbr0` is a bridge and stays UP when
  its only physical port vanishes, so the kernel never withdraws the wired
  default and traffic blackholes rather than failing over. Something has to
  probe. If the wired path is down *and* wifi has no gateway in its lease, the
  script changes nothing rather than stranding a host with no remote hands;
  `wifi-failover.selfcheck.sh` covers that branch.

  This moves the **host** off the dead uplink. It does not save the tunnel: the
  containers gateway via the LAN router, not via the host, so cloudflared in 102
  still goes down with the wired path. A cloudflared on the host is the piece
  that would fix that, and it is not built yet.
* **The camera needs a real browser.** aiortc gets ICE working after the
  printer's malformed SDP is sanitised, but DTLS never completes - the
  printer's `pear` stack ignores the ClientHello. Headless Chromium negotiates
  fine, so the relay drives one and re-serves MJPEG.
* **Cloudflare replaces origin 5xx** with its own error page, swallowing the
  body. Return 4xx for anything whose message the UI needs to show.
* **The USB backup drive needs two fixes, not one.** The JMicron 152d:0578
  bridge dropped off the bus mid-transfer in sessions that got shorter each
  time, always ending `Synchronize Cache(10) failed: hostbyte=DID_ERROR`.
  `usb-storage quirks=152d:0578:u` takes it off UAS, and that alone still died
  about 4G into an 8G read - it also has to be kept out of runtime autosuspend,
  which is what `udev/99-backup-enclosure.rules` does. It draws 896mA of a
  900mA SuperSpeed budget, so there is no headroom to survive a suspend/resume
  mid-transfer. With both applied: 8G read at 107 MB/s, 6G write at 80 MB/s,
  zero disconnects. The drive itself was never at fault - SMART passes with
  zero reallocated, pending and CRC counts.

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
