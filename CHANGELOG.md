# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Snapshot format v1: a directory with `manifest.json`, `fs/`, `commands/` and
  `journal.jsonl`, with per-file sha256 and byte-stable output.
- `sickbay capture DEST [--since S] [--no-scrub]`: collects `/proc`,
  `/sys/fs/cgroup`, `/etc` and `/run/systemd` evidence, allowlisted command
  output and the journal from a live systemd host. Scrubs hostnames, users,
  IPs, MACs, the machine id and `Environment=` values by default.
- `sickbay verify SNAPSHOT`: checks every size and hash and rejects stray files.
- Snapshot reader and the `HostView` protocol that later tools will read through.
- Three seed incidents (`fd-exhaustion`, `memory-ceiling-parent-slice`, `restart-loop`) with
  inject/cleanup scripts, draft labels and committed snapshots, and the evidence tests.
- `scripts/reproduce.sh` and the weekly re-capture workflow.

### Fixed

- `systemctl show` over every loaded unit failed on the root slice `-.slice`; units now follow `--`.
- Scrubbing: usernames match case-insensitively, `_` and `-` are word boundaries, each GECOS word
  is scrubbed, dash-form MACs and the FQDN domain part are handled, and replacement addresses
  never collide with real documentation-range addresses.
