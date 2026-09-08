#!/bin/bash
# Brings up wlp59s0 as a SECONDARY management path only.
# -G (nogateway): never install a default route; vmbr0/nic0 stays the uplink.
# -C resolv.conf: do not let this interface rewrite DNS.
IFACE=wlp59s0
pkill -f "[w]pa_supplicant.*$IFACE" 2>/dev/null
sleep 1
ip link set "$IFACE" up
wpa_supplicant -B -i "$IFACE" -c /etc/wpa_supplicant/wpa_supplicant.conf || exit 1
for i in $(seq 1 25); do
  iw dev "$IFACE" link 2>/dev/null | grep -q "Connected to" && break
  sleep 1
done
iw dev "$IFACE" link | grep -q "Connected to" || { echo "association failed"; exit 1; }
exec dhcpcd -G -b -C resolv.conf "$IFACE"
