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
LAN_GW=${LAN_GW:-10.20.1.1}
DRY_RUN=${DRY_RUN:-0}

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

gateway_ok() {
  if [ -n "${FAKE_GW_OK+x}" ]; then [ "$FAKE_GW_OK" = yes ]; return; fi
  ping -c1 -W2 -I "$BRIDGE" "$LAN_GW" >/dev/null 2>&1
}

main() {
  # The healthy path has to be cheap and silent - this runs on every udev net
  # event and every timer tick.
  if in_bridge && gateway_ok; then
    return 0
  fi

  if ! nic_exists; then
    # The controller has not finished re-enumerating, or it is gone. A PCI
    # rescan re-probes a controller that removed itself; it only ever adds
    # devices, so it cannot detach anything that is currently working.
    log "$NIC is absent; rescanning the PCI bus"
    run sh -c 'echo 1 > /sys/bus/pci/rescan'
    return 1
  fi

  if ! in_bridge; then
    log "$NIC is up but outside $BRIDGE; rejoining"
    run ip link set "$NIC" up
    run ip link set "$NIC" master "$BRIDGE"
    return 0
  fi

  # In the bridge but the gateway does not answer. Bouncing the link is the
  # cheapest thing that fixes a port stuck after a re-enumeration, and it is
  # safe: if this was not the problem, the next tick tries again.
  log "$NIC is in $BRIDGE but $LAN_GW is unreachable; bouncing the link"
  run ip link set "$NIC" down
  run ip link set "$NIC" up
  run ip link set "$NIC" master "$BRIDGE"
}

main "$@"
