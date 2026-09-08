#!/bin/bash
# Bracketed patterns ([w]pa...) so pkill cannot match this script's own cmdline.
IFACE=wlp59s0
pkill -f "[d]hcpcd.*$IFACE" 2>/dev/null
pkill -f "[w]pa_supplicant.*$IFACE" 2>/dev/null
sleep 1
ip addr flush dev "$IFACE" 2>/dev/null
ip link set "$IFACE" down 2>/dev/null
exit 0
