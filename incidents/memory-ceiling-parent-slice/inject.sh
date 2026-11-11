#!/usr/bin/env bash
# Exit codes: 0 ready; 1 failed (cleaned up); 10 refused because something already exists
# or the host is not suitable (nothing was changed).
# Put a memory ceiling on a slice, start a service inside it that wants more
# than the ceiling (the service has no limit of its own), and wait until the
# kernel has killed it. Acts only through a runtime slice file under /run and a
# transient systemd unit.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit=report-builder.service
slice=batch.slice
slice_file=/run/systemd/system/$slice
dir=/run/report-svc
timeout=60

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# A .slice always loads (systemd synthesizes it), so test its state, not LoadState.
if [ "$(systemctl show -p LoadState --value "$unit")" != "not-found" ] ||
    [ "$(systemctl show -p ActiveState --value "$slice")" != "inactive" ] ||
    [ -e "$slice_file" ] || [ -e "$dir" ]; then
    echo "refusing: $unit, $slice or their files already exist" >&2
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
cat >"$slice_file" <<'UNIT'
[Unit]
Description=Batch workloads

[Slice]
MemoryMax=64M
MemorySwapMax=0
UNIT
shield systemctl daemon-reload

cat >"$dir/build.py" <<'PY'
import time

print("report-builder: assembling report", flush=True)
parts = []
for _ in range(200):
    parts.append(bytes(bytearray(b"\x01") * (1024 * 1024)))
    time.sleep(0.01)
print("report-builder: report ready", flush=True)
time.sleep(3600)
PY

shield systemd-run --quiet --unit="${unit%.service}" --slice="$slice" \
    --description="Report builder" -p Restart=no \
    /usr/bin/python3 "$dir/build.py"

deadline=$((SECONDS + timeout))
while :; do
    if [ "$interrupted" -ne 0 ]; then
        echo "interrupted" >&2
        exit 130
    fi
    active=$(systemctl show -p ActiveState --value "$unit")
    result=$(systemctl show -p Result --value "$unit")
    if [ "$active" = "failed" ] && [ "$result" = "oom-kill" ]; then
        break
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
        echo "timed out: state=$active result=$result" >&2
        exit 1
    fi
    sleep 1
done

finished=1
echo "ready: $unit"
