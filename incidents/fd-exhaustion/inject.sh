#!/usr/bin/env bash
# Exit codes: 0 ready; 1 failed (cleaned up); 10 refused because something already exists
# or the host is not suitable (nothing was changed).
# Start a service that holds every file descriptor its limit allows, then keeps
# running and logging the failures. Blocks until that state is reached.
# Acts only through a transient systemd unit and files under /run.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit=ledger-api.service
dir=/run/ledger-svc
spool=/run/ledger-spool
limit=512
timeout=60

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

state=$(systemctl show -p LoadState --value "$unit")
if [ "$state" != "not-found" ] || [ -e "$dir" ] || [ -e "$spool" ]; then
    echo "refusing: $unit or its files already exist" >&2
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

mkdir -p "$dir" "$spool"
cat >"$dir/serve.py" <<'PY'
import time

spool = "/run/ledger-spool"
held = []
n = 0
print("ledger-api: started", flush=True)
while True:
    try:
        held.append(open(f"{spool}/{n:05d}.dat", "w"))
        n += 1
    except OSError as exc:
        print(f"ledger-api: spool write failed: {exc}", flush=True)
        time.sleep(2)
PY

shield systemd-run --quiet --unit="${unit%.service}" --description="Ledger API worker" \
    -p LimitNOFILE="$limit" -p Restart=no \
    /usr/bin/python3 "$dir/serve.py"

deadline=$((SECONDS + timeout))
while :; do
    if [ "$interrupted" -ne 0 ]; then
        echo "interrupted" >&2
        exit 130
    fi
    pid=$(systemctl show -p MainPID --value "$unit")
    if [ "$pid" -gt 0 ] && [ -d "/proc/$pid/fd" ]; then
        count=$(find "/proc/$pid/fd" -mindepth 1 | wc -l)
        logs=$(journalctl -u "$unit" --no-pager -o cat)
        if [ "$count" -ge "$limit" ] && [[ $logs == *"Too many open files"* ]]; then
            break
        fi
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "timed out waiting for the service to reach its limit" >&2
        exit 1
    fi
    sleep 1
done

finished=1
echo "ready: $unit"
