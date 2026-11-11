#!/usr/bin/env bash
# Remove everything inject.sh created. Safe to run any number of times.
set -euo pipefail

# Finish even if the caller's process group is signalled mid-run.
trap '' INT TERM HUP

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

systemctl stop report-builder.service 2>/dev/null || true
systemctl reset-failed report-builder.service 2>/dev/null || true
systemctl stop batch.slice 2>/dev/null || true
systemctl reset-failed batch.slice 2>/dev/null || true
rm -rf /run/report-svc /run/systemd/system/batch.slice /run/systemd/system/batch.slice.d
systemctl daemon-reload
exit 0
