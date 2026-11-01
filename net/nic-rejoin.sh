#!/bin/bash
# Put the USB NIC back into vmbr0 after it re-enumerates.
#
# The Alpine Ridge controller (0000:3a:00.0) removes itself and takes every
# device on it down with it - 26 times between 2026-09-02 and 2026-09-08. The
# devices come back on their own within about two seconds. The NIC does not
# come back inside the bridge, and nothing puts it there.
#
# That single missing step is the whole outage: vmbr0 loses its only port, so
# every container loses the LAN, both cloudflared tunnels drop, and the box goes
# dark. On 2026-09-08 that cost 30 minutes and needed someone at the console to
# type two commands. This is those two commands, run automatically.
#
# It is the same fix as the dashboard's nic_rejoin button
# (exporter/pve-exporter.py), which stays for the case where this has not fired.
#
# Driven two ways, because each covers what the other misses:
#   udev  - fires the instant the interface appears. Handles the common case.
#   timer - a backstop for a bridge that lost its port some other way, or a
#           device that never came back and needs the bus rescanned.
set -u

NIC=${NIC:-nic0}
BRIDGE=${BRIDGE:-vmbr0}
DRY_RUN=${DRY_RUN:-0}
# Last rx_packets reading, so each tick can tell whether anything arrived since.
RX_STATE=${RX_STATE:-/run/nic-rejoin.rx}
NET_SYS=${NET_SYS:-/sys/class/net}   # overridable for the self-check

# The controller everything hangs off. Specific to this machine: the Anker hub
# is in the USB-C port, which is Alpine Ridge, and that is the part that fails.
# Change this if the hub moves to a PCH port (00:14.0).
XHCI=${XHCI:-0000:3a:00.0}
COUNT=${COUNT:-/run/nic-rejoin.absent}
# 3 checks at 20s is about a minute of being gone before the heavy fix. Long
# enough not to fire during a normal re-enumeration, which takes ~2 seconds.
ESCALATE_AFTER=${ESCALATE_AFTER:-3}

log() { logger -t nic-rejoin "$*"; echo "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "WOULD: $*"; else "$@"; fi; }

# Overridable so the self-check can drive each branch without real hardware.
nic_exists() {
  if [ -n "${FAKE_NIC_EXISTS+x}" ]; then [ "$FAKE_NIC_EXISTS" = yes ]; return; fi
  [ -e "/sys/class/net/$NIC" ]
}

in_bridge() {
  if [ -n "${FAKE_IN_BRIDGE+x}" ]; then [ "$FAKE_IN_BRIDGE" = yes ]; return; fi
  [ -e "/sys/class/net/$BRIDGE/brif/$NIC" ]
}

# Is the wire actually carrying traffic? Received packets since the last tick,
# not a gateway ping. Until 2026-09-30 this pinged 10.20.1.1; then the port was
# moved onto the Art-Net lighting network (2.0.0.0/8), where the host has no
# address and no gateway can answer it, so it bounced the lighting link 40 times
# in six minutes. A counter works on any network: the lighting side receives
# ~300 packets/s and the office LAN is never silent for 20s either.
#
# The first reading after boot, or after the state file is lost, counts as
# alive - there is nothing to compare it with yet.
link_alive() {
  if [ -n "${FAKE_LINK_ALIVE+x}" ]; then [ "$FAKE_LINK_ALIVE" = yes ]; return; fi
  local now prev
  now=$(cat "$NET_SYS/$NIC/statistics/rx_packets" 2>/dev/null) || return 1
  prev=$(cat "$RX_STATE" 2>/dev/null || echo "")
  echo "$now" > "$RX_STATE"
  [ -z "$prev" ] || [ "$now" != "$prev" ]
}

main() {
  # The healthy path has to be cheap and silent - this runs on every udev net
  # event and every timer tick.
  if in_bridge && link_alive; then
    return 0
  fi

  if ! nic_exists; then
    # The controller has not finished re-enumerating, or it is gone. A PCI
    # rescan re-probes a controller that removed itself; it only ever adds
    # devices, so it cannot detach anything that is currently working.
    log "$NIC is absent; rescanning the PCI bus"
    run sh -c 'echo 1 > /sys/bus/pci/rescan'

    # Escalation. On 2026-09-09 the controller stayed present and bound while
    # every device behind it - hub, NIC and backup drive - was gone, and rescans
    # changed nothing for hours. Unbinding and rebinding xhci_hcd brought all
    # three back at once. That is the only thing that has ever fixed this state
    # short of someone unplugging the hub.
    #
    # Gated on a counter because a rebind is heavy, and gated on $NIC being
    # absent because that is what makes it safe: if the NIC is not there, the
    # controller is not carrying our only network path, so resetting it costs
    # nothing. Never do this while the NIC is alive.
    local n=0
    [ -r "$COUNT" ] && n=$(cat "$COUNT" 2>/dev/null || echo 0)
    n=$((n + 1))
    run sh -c "echo $n > $COUNT"
    if [ "$n" -ge "$ESCALATE_AFTER" ]; then
      log "$NIC absent for $n checks; unbinding and rebinding $XHCI"
      run sh -c "echo $XHCI > /sys/bus/pci/drivers/xhci_hcd/unbind 2>/dev/null"
      run sleep 4
      run sh -c "echo $XHCI > /sys/bus/pci/drivers/xhci_hcd/bind 2>/dev/null"
      run sh -c "echo 0 > $COUNT"
    fi
    return 1
  fi

  # Present again - forget any escalation history.
  [ -e "$COUNT" ] && run rm -f "$COUNT"

  if ! in_bridge; then
    log "$NIC is up but outside $BRIDGE; rejoining"
    run ip link set "$NIC" up
    run ip link set "$NIC" master "$BRIDGE"
    return 0
  fi

  # In the bridge but nothing received for a whole tick. Bouncing the link is
  # the cheapest thing that fixes a port stuck after a re-enumeration, and it is
  # safe: if this was not the problem, the next tick tries again.
  log "$NIC is in $BRIDGE but received nothing since the last check; bouncing the link"
  run ip link set "$NIC" down
  run ip link set "$NIC" up
  run ip link set "$NIC" master "$BRIDGE"
}

main "$@"
