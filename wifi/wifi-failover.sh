#!/bin/bash
# Move the host's default route to wifi when the wired gateway stops answering,
# and move it back when it returns.
#
# Why this probes instead of just setting a route metric: vmbr0 is a bridge, and
# it stays UP even when its only physical port - the USB NIC - vanishes. The
# kernel therefore never withdraws the wired default route, so a higher-metric
# wifi route would never win and traffic would blackhole instead of failing
# over. That is the exact failure this host has already had. Something has to
# actively test the path and swap.
#
# wifi-up.sh runs dhcpcd with -G so wifi never carries a default route in normal
# operation. This is the one thing allowed to add one, and only while the wired
# path is down.
set -u

LAN_GW=${LAN_GW:-10.20.1.1}
LAN_IF=${LAN_IF:-vmbr0}
WIFI_IF=${WIFI_IF:-wlp59s0}
TRIES=${TRIES:-3}
STATE=${STATE:-/run/wifi-failover.failed}
DRY_RUN=${DRY_RUN:-0}

log() { logger -t wifi-failover "$*"; echo "$*"; }
run() { if [ "$DRY_RUN" = 1 ]; then echo "WOULD: $*"; else "$@"; fi; }

# Overridable so the self-check can drive both branches without real hardware.
wired_ok() {
  if [ -n "${FAKE_WIRED+x}" ]; then [ "$FAKE_WIRED" = ok ]; return; fi
  local i
  for i in $(seq 1 "$TRIES"); do
    ping -c1 -W2 -I "$LAN_IF" "$LAN_GW" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# The gateway comes from the live lease rather than a constant: this laptop
# moves between networks, and a stale hardcoded address would fail over to
# nowhere.
wifi_gw() {
  # Tested for being SET, not for being non-empty. "set but empty" is exactly
  # the case the self-check needs to simulate - a lease with no gateway - and a
  # -n test would fall through to the real dhcpcd instead, which on the host
  # answers with a live gateway and silently turns that check into a no-op.
  if [ -n "${FAKE_WIFI_GW+x}" ]; then echo "$FAKE_WIFI_GW"; return; fi
  dhcpcd -U "$WIFI_IF" 2>/dev/null | sed -n 's/^routers=//p' | awk '{print $1}'
}

failed_over() { [ -e "$STATE" ]; }

main() {
  if wired_ok; then
    failed_over || return 0
    # Add the wired route back before removing the wifi one, so there is never
    # an instant with no default route at all.
    run ip route replace default via "$LAN_GW" dev "$LAN_IF" onlink
    run ip route del default dev "$WIFI_IF" 2>/dev/null
    run rm -f "$STATE"
    log "wired gateway $LAN_GW answering again; default route back on $LAN_IF"
    return 0
  fi

  failed_over && return 0

  local gw
  gw=$(wifi_gw)
  if [ -z "$gw" ]; then
    log "wired gateway $LAN_GW is down but $WIFI_IF has no gateway in its lease; leaving the route alone"
    return 1
  fi

  # Same ordering rule in reverse: install the replacement first.
  run ip route replace default via "$gw" dev "$WIFI_IF" metric 100
  run ip route del default dev "$LAN_IF" 2>/dev/null
  run touch "$STATE"
  log "wired gateway $LAN_GW unreachable; default route moved to $WIFI_IF via $gw"
}

main "$@"
