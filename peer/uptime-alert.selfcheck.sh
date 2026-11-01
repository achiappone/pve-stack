#!/bin/bash
# Runnable check for uptime-alert.sh: fake curl and sendmail, throwaway state.
#   bash host/uptime-alert.selfcheck.sh
set -u
D=$(mktemp -d); trap 'rm -rf "$D"' EXIT
SUT="$(cd "$(dirname "$0")" && pwd)/uptime-alert.sh"

echo "http://x/ thing" > "$D/list"
printf '#!/bin/sh\ncat "$CODE_FILE"\n' > "$D/curl"
printf '#!/bin/sh\ngrep "^Subject:" >> "$MAILS"\n' > "$D/sendmail"
chmod +x "$D/curl" "$D/sendmail"
export CODE_FILE="$D/code" MAILS="$D/mails"
: > "$MAILS"

run() { echo "$1" > "$CODE_FILE"
        LIST="$D/list" STATE="$D/state" FAILS=3 CURL="$D/curl" SENDMAIL="$D/sendmail" bash "$SUT"; }
mails() { wc -l < "$MAILS" | tr -d ' '; }
n() { grep -c "$1" "$MAILS"; }
bad=0
expect() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1 (got $2, want $3)"; bad=$((bad + 1)); fi; }

echo "uptime-alert self-check"
run 200;                expect "up -> no mail"                    "$(mails)" 0
run 000; run 000;       expect "two misses -> still quiet"        "$(mails)" 0
run 000;                expect "third miss -> one DOWN mail"      "$(n DOWN)" 1
run 000; run 000;       expect "stays down -> no repeat"          "$(n DOWN)" 1
run 401;                expect "any HTTP answer -> back-up mail"  "$(n 'back up')" 1
run 000; run 200;       expect "single blip -> no mail"           "$(mails)" 2

# Heartbeat entries: up while the file is fresh, down once it goes stale.
echo "file:$D/beat peer" > "$D/list"; : > "$MAILS"
touch "$D/beat";        run x; expect "fresh heartbeat -> no mail"      "$(mails)" 0
touch -t 202001010000 "$D/beat"
run x; run x; run x;    expect "stale heartbeat -> DOWN mail"     "$(n DOWN)" 1
touch "$D/beat";        run x; expect "heartbeat resumes -> back-up mail" "$(n 'back up')" 1

[ "$bad" -eq 0 ] && echo "all checks passed" || { echo "$bad check(s) failed"; exit 1; }
