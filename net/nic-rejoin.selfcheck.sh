#!/bin/bash
# Self-check for nic-rejoin.sh. Drives every branch in DRY_RUN with the three
# probes faked, so the logic can be exercised without pulling the dongle out.
#
# The first case is the one that runs thousands of times a day: healthy must do
# nothing at all. The rest are the outage of 2026-09-08.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SUT=$HERE/nic-rejoin.sh
fails=0

check() {
  local name=$1 expect=$2 out
  shift 2
  out=$("$@" 2>&1)
  if echo "$out" | grep -q -- "$expect"; then
    echo "  ok   $name"
  else
    echo "  FAIL $name"
    echo "       expected to match: $expect"
    echo "       got: $out"
    fails=$((fails + 1))
  fi
}

refute() {
  local name=$1 forbid=$2 out
  shift 2
  out=$("$@" 2>&1)
  if echo "$out" | grep -q -- "$forbid"; then
    echo "  FAIL $name (should not have done: $forbid)"
    echo "       got: $out"
    fails=$((fails + 1))
  else
    echo "  ok   $name"
  fi
}

echo "nic-rejoin self-check"

refute "healthy -> touches nothing" "WOULD:" \
  env DRY_RUN=1 FAKE_IN_BRIDGE=yes FAKE_GW_OK=yes bash "$SUT"

check "nic present, outside the bridge -> rejoins" "WOULD: ip link set nic0 master vmbr0" \
  env DRY_RUN=1 FAKE_NIC_EXISTS=yes FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

check "nic absent -> rescans the pci bus" "/sys/bus/pci/rescan" \
  env DRY_RUN=1 FAKE_NIC_EXISTS=no FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

refute "nic absent -> does NOT try to add a device that is not there" "ip link set nic0 master" \
  env DRY_RUN=1 FAKE_NIC_EXISTS=no FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

check "in bridge but gateway dead -> bounces the link" "WOULD: ip link set nic0 down" \
  env DRY_RUN=1 FAKE_NIC_EXISTS=yes FAKE_IN_BRIDGE=yes FAKE_GW_OK=no bash "$SUT"

C=$(mktemp -u /tmp/nic-rejoin-selfcheck.XXXXXX)

rm -f "$C"
refute "absent, first check -> rescans but does NOT rebind yet" "unbind" \
  env DRY_RUN=1 COUNT="$C" FAKE_NIC_EXISTS=no FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

echo 2 > "$C"
check "absent, third check -> escalates to a controller rebind" "unbind" \
  env DRY_RUN=1 COUNT="$C" FAKE_NIC_EXISTS=no FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

echo 9 > "$C"
refute "nic ALIVE -> never rebinds the controller, whatever the counter says" "unbind" \
  env DRY_RUN=1 COUNT="$C" FAKE_NIC_EXISTS=yes FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"

echo 9 > "$C"
check "nic back -> clears the escalation counter" "rm -f" \
  env DRY_RUN=1 COUNT="$C" FAKE_NIC_EXISTS=yes FAKE_IN_BRIDGE=no FAKE_GW_OK=no bash "$SUT"
rm -f "$C"

echo
if [ "$fails" -eq 0 ]; then echo "all checks passed"; else echo "$fails check(s) failed"; exit 1; fi
