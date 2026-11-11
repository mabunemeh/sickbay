#!/usr/bin/env bash
# Re-create an incident on this host and capture its snapshot.
#
#   scripts/reproduce.sh <incident-id> <dest> [--update-committed]
#
# Runs incidents/<id>/inject.sh (blocks until the fault has manifested), captures
# into <dest> (must not exist or be empty), and runs cleanup.sh afterwards, whatever
# happens, except when inject.sh refused to start (exit 10, nothing was changed).
# With --update-committed the capture is also copied over incidents/<id>/snapshot/.
#
# Needs passwordless sudo. The sickbay binary is $SICKBAY_BIN, else
# ~/venvs/sickbay/bin/sickbay, else whatever is on PATH.
set -euo pipefail

usage() {
    echo "usage: $0 <incident-id> <dest> [--update-committed]" >&2
    exit 2
}

[ $# -ge 2 ] && [ $# -le 3 ] || usage
id=$1
dest=$2
update=0
if [ $# -eq 3 ]; then
    [ "$3" = "--update-committed" ] || usage
    update=1
fi
case $id in
    *[!a-z0-9-]* | "") echo "bad incident id: $id" >&2; exit 2 ;;
esac

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
inc=$repo/incidents/$id
[ -x "$inc/inject.sh" ] && [ -x "$inc/cleanup.sh" ] || {
    echo "incident not found or scripts not executable: $inc" >&2
    exit 2
}

bin=${SICKBAY_BIN:-}
if [ -z "$bin" ] && [ -x "$HOME/venvs/sickbay/bin/sickbay" ]; then
    bin=$HOME/venvs/sickbay/bin/sickbay
fi
[ -n "$bin" ] || bin=$(command -v sickbay || true)
[ -n "$bin" ] && [ -x "$bin" ] || { echo "sickbay binary not found; set SICKBAY_BIN" >&2; exit 2; }

if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
    echo "destination exists and is not empty: $dest" >&2
    exit 2
fi

work=$(mktemp -d)
need_cleanup=0
finish() {
    trap '' INT TERM HUP
    if [ "$need_cleanup" -eq 1 ]; then
        need_cleanup=0
        sudo "$inc/cleanup.sh" || echo "cleanup.sh failed" >&2
    fi
    sudo rm -rf "$work"
}
trap finish EXIT

start=$(date +%s)
# inject.sh exits 10 when it refuses to start (something already exists): it changed nothing,
# so running cleanup.sh here could delete someone else's files. Any other outcome cleans up.
set +e
sudo "$inc/inject.sh"
rc=$?
set -e
if [ "$rc" -eq 10 ]; then
    echo "inject refused; nothing was changed and nothing will be cleaned up" >&2
    exit 10
fi
need_cleanup=1
[ "$rc" -eq 0 ] || exit "$rc"

since=$(date -d "@$((start - 60))" '+%Y-%m-%d %H:%M:%S')
sudo "$bin" capture "$work/cap" --since="$since"
sudo chown -R "$(id -u):$(id -g)" "$work/cap"

mkdir -p "$dest"
cp -a "$work/cap/." "$dest/"
if [ "$update" -eq 1 ]; then
    rm -rf "$inc/snapshot"
    mkdir -p "$inc/snapshot"
    cp -a "$work/cap/." "$inc/snapshot/"
fi
echo "captured $id -> $dest"
