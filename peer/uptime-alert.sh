#!/bin/bash
# Email when a watched URL stops answering, and again when it comes back.
# Run every minute by uptime-alert.timer.
#
# Any HTTP status counts as up - the dashboard's probes use the same rule: a
# 401 from a login page is a live server. "Down" means FAILS checks in a row
# with no answer at all, so one dropped request is not an email.
#
# A `file:/path` entry is a heartbeat instead: up while the file was rewritten
# in the last MAXAGE minutes. That is how each box watches the other - the
# peer's peer-sync.timer refreshes /var/log/peer/<peer>/status.json every 5.
set -u
LIST=${LIST:-/etc/uptime-alert.list}
STATE=${STATE:-/var/lib/uptime-alert}
FAILS=${FAILS:-3}
MAXAGE=${MAXAGE:-15}                    # minutes, for file: heartbeats
DASH=${DASH:-}                          # this box's dashboard, linked in the mail
TO=${TO:-root}                          # aliased to the gmail address in /etc/aliases
CURL=${CURL:-curl}                      # overridable for the self-check
SENDMAIL=${SENDMAIL:-/usr/sbin/sendmail}
HOST=$(hostname)

mkdir -p "$STATE"

mail() {  # subject, body
  printf 'To: %s\nSubject: %s\n\n%s\n%s\n' "$TO" "$1" "$2" \
    "${DASH:+
Dashboard: $DASH}" | $SENDMAIL -t
}

while read -r url name; do
  case "$url" in ''|\#*) continue ;; esac
  name=${name:-$url}
  f="$STATE/$(printf '%s' "$url" | tr -c 'A-Za-z0-9' _)"
  fails=0; down=0
  [ -f "$f" ] && read -r fails down < "$f"

  if [ "${url#file:}" != "$url" ]; then
    [ -n "$(find "${url#file:}" -mmin -"$MAXAGE" 2>/dev/null)" ] && code=fresh || code=000
  else
    code=$($CURL -sk -o /dev/null -m 10 -w '%{http_code}' "$url" 2>/dev/null)
  fi
  if [ -n "$code" ] && [ "$code" != 000 ]; then
    [ "$down" = 1 ] && mail "$HOST: $name is back up" \
      "$url is OK again ($code) as of $(date '+%a %d %b %H:%M %Z')."
    fails=0; down=0
  else
    fails=$((fails + 1))
    if [ "$fails" -ge "$FAILS" ] && [ "$down" = 0 ]; then
      mail "$HOST: $name is DOWN" \
        "$url has failed $fails checks in a row, as of $(date '+%a %d %b %H:%M %Z').
You will get one more email when it is back."
      down=1
    fi
  fi
  echo "$fails $down" > "$f"
done < "$LIST"
