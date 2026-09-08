#!/bin/bash
# Self-check for wifi-failover.sh. Runs every branch in DRY_RUN with the two
# probes faked, so the decision logic can be exercised without a wired outage.
#
#   ./wifi-failover.selfcheck.sh
#
# The case that matters most is the last one: a wired outage while wifi has no
# gateway must leave the routing table untouched. Getting that wrong strands a
# host that has no remote hands.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
SUT=$HERE/wifi-failover.sh
STATE=$(mktemp -u /tmp/wifi-failover-selfcheck.XXXXXX)
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

echo "wifi-failover self-check"

rm -f "$STATE"
check "wired up, not failed over -> does nothing" "^$" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=ok bash "$SUT"

touch "$STATE"
check "wired back, was failed over -> restores vmbr0" "WOULD: ip route replace default via 10.20.1.1 dev vmbr0 onlink" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=ok bash "$SUT"

rm -f "$STATE"
check "wired down, wifi gw known -> fails over" "WOULD: ip route replace default via 10.20.5.1 dev wlp59s0 metric 100" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=down FAKE_WIFI_GW=10.20.5.1 bash "$SUT"

rm -f "$STATE"
check "wired down, wifi gw known -> removes the dead wired route" "WOULD: ip route del default dev vmbr0" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=down FAKE_WIFI_GW=10.20.5.1 bash "$SUT"

rm -f "$STATE"
check "wired down, NO wifi gw -> leaves routing alone" "leaving the route alone" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=down FAKE_WIFI_GW="" bash "$SUT"

rm -f "$STATE"
out=$(env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=down FAKE_WIFI_GW="" bash "$SUT" 2>&1)
if echo "$out" | grep -q "ip route del"; then
  echo "  FAIL wired down, NO wifi gw -> must not delete anything"
  fails=$((fails + 1))
else
  echo "  ok   wired down, NO wifi gw -> deletes nothing"
fi

touch "$STATE"
check "wired down, already failed over -> does nothing" "^$" \
  env DRY_RUN=1 STATE="$STATE" FAKE_WIRED=down FAKE_WIFI_GW=10.20.5.1 bash "$SUT"

rm -f "$STATE"
echo
if [ "$fails" -eq 0 ]; then echo "all checks passed"; else echo "$fails check(s) failed"; exit 1; fi
