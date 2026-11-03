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
    printer/                k2-watch: finds the K2 printer and keeps CT 101/102 pointed at it
    net/                    NIC auto-rejoin            nic-rejoin + self-check

Monitoring lives in 103 (beszel), 104 (uptime-kuma) and 105 (pulse). Those are
installed from upstream releases rather than from this repo, but each binds only
its vmbr1 address, so the ops tunnel is the one way in and nothing of theirs is
on the office LAN.

Backups go to a 128G SD card (ext4, label `pve-backup-sd`) in the built-in
reader, mounted at `/mnt/backup` and registered as the `backup-sd` storage with
`is_mountpoint 1` - without that flag a run while the card is unmounted writes
into the empty directory and fills the root filesystem. The fstab entry is
`nofail`: this host has no remote hands, and a card that fails to appear must
never hold up boot. It replaced the USB disk below, which went missing on
2026-09-09; the job then failed quietly every night until 2026-09-30.

The K2 printer dashboard itself lives in its own repo,
`github.com/achiappone/k2plus-dashboard`, and is deployed by `deploy/k2-deploy`.

## Paired with the home box

The home Proxmox box (`promox`, HP 250 G7) has its own repo,
`achiappone/home-devops`. The two boxes check in with each other every 5
minutes over `peer/peer-sync.sh`:

- Each dashboard has an *Other box* health panel and a link to the other one.
- Each box's `uptime-alert` emails if the other goes 15 minutes without
  checking in.
- The receiving side pins each box's key to a write-only `rrsync` directory,
  `/var/log/peer/<sender>`.

`home-devops`'s README has the details.

This box's mail relays through Gmail (`smtp.gmail.com:587`, credentials in
`/etc/postfix/sasl_passwd`), set up 2026-10-31. Before that, every message
bounced. Root mail reaches the gmail address through Proxmox's
`/root/.forward`.

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

## K2 printer watchdog

`printer/k2-watch.py` runs on the host every minute (`k2-watch.timer`). It
recognises the printer by its Moonraker hostname (`K2Plus-05E6`, in
`printer/k2-watch.conf`) and keeps CT 101 (`PRINTER_HOST` in
`/etc/k2-dashboard.env`) and CT 102 (`PRINTER_URL` in `/etc/pve-ops.env`)
pointed at it.

- **On the lighting network** (normal) it only rewrites those addresses when
  the printer's DHCP address changes. Routing on the host stays **off**
  (`vmbr0` and `wlp59s0` forwarding = 0). That also closes a path that existed
  from 2026-11-01, when the host got 2.0.0.43: a lighting-network device could
  route through it into vmbr1.
- **On the office wifi** (the printer's fallback when its cable has no link;
  it sat there from about 2026-10-30 to 11-03 with both dashboards blind) the
  containers cannot reach it. The host routes them to the printer's address
  and nothing else: each CT gets a `/32` route via 2.0.0.43, the host NATs it
  out `wlp59s0`, and the `K2WATCH` chains drop every other routed packet in
  or out of wifi or off the lighting network. It is routing, not a port
  relay, so the WebRTC camera and the ssh repair actions work as well.
- Every move is emailed. A printer that is switched off is not a move:
  nothing changes. It searches known addresses (lighting first), then the
  lighting /24 at most every 10 min, then the wifi /24 at most every 6 h
  (it's the office network; sweeping it every minute would look like a port
  scan). State lives in `/var/lib/k2-watch/state.json`, and
  `k2-watch.py --selfcheck` tests the search order.

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

  That moves the **host** off the dead uplink, which on its own saves nothing:
  the containers gateway via the LAN router rather than via the host, so
  cloudflared in 102 goes down with the wired path regardless. The piece that
  closes it is a *second* cloudflared running on the host itself
  (`cloudflared/host.yml`, tunnel `pve-host`, hostname
  `pve-direct.anthonychiappone.com`). It uses the host's routing table, so it
  follows the failover onto wifi. Verified end to end on 2026-09-08: with the
  default route forced onto `wlp59s0`, an ssh to `pve-direct` still lands.

  It reaches the host only, not the containers - but the host is where `nic0`
  gets fixed from, so that is the one that matters.

  **Since 2026-11-01 the wired gateway is the lighting network's router,
  2.0.0.2**, and the host has 2.0.0.43/8 on `vmbr0` (beside the old
  10.20.1.43/24). From 2026-09-30, when the wire moved to Art-Net, until then,
  the failover still probed the old office router 10.20.1.1, so the host sat
  on wifi for a month. DNS is set through Proxmox (node -> System -> DNS):
  1.1.1.1, 8.8.8.8, then Chauvet's 10.10.1.4 last. After any DNS change, run
  `systemctl restart postfix`: its chroot keeps its own copy of
  `resolv.conf`, and the stale copy (Chauvet's servers only) held every email
  in the queue from 2026-11-01 until it was noticed on 11-03. Chauvet's servers do not
  answer over the lighting network, and the public ones answer over both
  paths. Nothing blocks the lighting network from reaching 2.0.0.43; that was
  the user's choice. `/etc/network/interfaces` is not in this repo; the
  pre-change copy is `interfaces.bak-lighting` on the host.

* **The USB NIC comes back outside the bridge, and nothing put it back.** The
  Alpine Ridge controller `0000:3a:00.0` removes itself and takes every device
  on it with it - 26 times in six days. The devices re-enumerate within about
  two seconds; the NIC just does not rejoin `vmbr0`. That one missing step took
  the box off the network for half an hour on 2026-09-08 and needed someone at
  the console. `udev/99-nic-rejoin.rules` now fires the moment the interface
  appears, with `nic-rejoin.timer` as a backstop for the cases udev cannot see.
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
