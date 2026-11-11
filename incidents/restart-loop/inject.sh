#!/usr/bin/env bash
# Exit codes: 0 ready; 1 failed (cleaned up); 10 refused because something already exists
# or the host is not suitable (nothing was changed).
# Start a service whose configuration file is missing, so it exits straight away
# and systemd keeps restarting it. Blocks until it has restarted several times.
# Acts only through a transient systemd unit and files under /run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit=quote-sync.service
dir=/run/quote-sync
want=5
timeout=90

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

if [ "$(systemctl show -p LoadState --value "$unit")" != "not-found" ] || [ -e "$dir" ]; then
    echo "refusing: $unit or $dir already exist" >&2
    exit 10
fi

finished=0
undo() {
    if [ "$finished" -ne 1 ]; then
        # A group-wide signal must not interrupt the cleanup below (ignored signals are
        # inherited by cleanup.sh and the systemctl calls it makes).
        trap '' INT TERM HUP
        "$here/cleanup.sh" || true
    fi
}
trap undo EXIT

# A signal (also a group-wide one) only sets a flag: the steps below run to completion and the
# wait loop then exits, so undo never races a half-created unit. shield() makes the commands
# that talk to systemd immune, because ignored signals are inherited across exec.
interrupted=0
trap 'interrupted=1' INT TERM HUP
shield() { ( trap '' INT TERM HUP; "$@" ); }

mkdir -p "$dir"
cat >"$dir/sync.py" <<'PY'
import sys

conf = "/run/quote-sync/sync.conf"
try:
    open(conf).close()
except OSError as exc:
    print(f"quote-sync: cannot load settings: {exc}", flush=True)
    sys.exit(1)
PY

shield systemd-run --quiet --unit="${unit%.service}" --description="Quote sync" \
    -p Restart=always -p RestartSec=2 -p StartLimitIntervalSec=0 \
    /usr/bin/python3 "$dir/sync.py"

deadline=$((SECONDS + timeout))
while :; do
    if [ "$interrupted" -ne 0 ]; then
        echo "interrupted" >&2
        exit 130
    fi
    n=$(systemctl show -p NRestarts --value "$unit")
    if [ "${n:-0}" -ge "$want" ]; then
        break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "timed out: restarts=${n:-0}" >&2
        exit 1
    fi
    sleep 1
done

finished=1
echo "ready: $unit"
