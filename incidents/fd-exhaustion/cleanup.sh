#!/usr/bin/env bash
# Remove everything inject.sh created. Safe to run any number of times.
set -euo pipefail

# Finish even if the caller's process group is signalled mid-run.
trap '' INT TERM HUP

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

unit=ledger-api.service

systemctl stop "$unit" 2>/dev/null || true
systemctl reset-failed "$unit" 2>/dev/null || true
rm -rf /run/ledger-svc /run/ledger-spool
systemctl daemon-reload
exit 0
