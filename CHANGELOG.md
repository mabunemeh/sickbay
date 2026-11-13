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
- Tools (`sickbay.tools`, `TOOLSET_VERSION = 1`): `list_units`, `unit_status`,
  `journal_query`, `process_list`, `process_limits`, `cgroup_tree`, `disk_usage`, `sockets`,
  `read_file` and `list_dir`, read-only over the `HostView` protocol. One entry point,
  `call_tool(view, name, args)`, returns a JSON object serialised deterministically (sorted
  keys, stable row order). Values are passed through verbatim (no unit conversion).
  - Each tool has a JSON Schema (`additionalProperties: false`); arguments are validated by
    hand against it. String arguments are capped at 256 bytes and may not hold control
    characters.
  - Tool problems are results, `{"error": {"kind", "message"}}`, with kinds `unknown_tool`,
    `invalid_argument`, `not_allowed`, `not_found`, `is_symlink`, `is_directory`,
    `not_a_directory`, `unavailable` and `output_too_large`. A corrupt snapshot still raises.
  - Caps: a result is at most 8,192 bytes of UTF-8 (trailing rows are dropped, else
    `output_too_large`); `limit` defaults to 50, max 200; `cgroup_tree` returns at most 100
    nodes; journal messages are cut at 1,024 bytes, ps args at 512, other values at 2,048,
    on a UTF-8 boundary with a `…[+N bytes]` marker; `read_file` pages with `offset` and
    `max_bytes` (default 4,096, min 4, max 6,000); a page ends before a valid UTF-8
    character it would split, and `returned` is the exact byte count, so `offset +=
    returned` reassembles the file. Every capped result says `truncated: true` with `total`
    and `returned`.
  - `unit_status` keeps a key seen once as a string and a repeated key (several
    `ExecStart=` lines) as the list of its values in output order.
  - Path allowlist (`tools/paths.py`), checked before the view is touched: `/proc/meminfo`,
    `loadavg`, `uptime`, `/proc/pressure/**`, `/proc/sys/fs/**`,
    `/proc/<pid>/{limits,status,cmdline,cgroup,oom_score,fd}`, `/sys/fs/cgroup/**`,
    `/etc/systemd/**`, `/etc/security/limits.conf`, `/etc/security/limits.d/**`,
    `/etc/os-release` and `/run/systemd/{system,transient,system.control}/**`. Paths must be
    absolute and normalised; relative paths, `.`/`..`/empty components, a trailing slash,
    `%`, NUL, control characters and paths over 256 bytes are rejected, not normalised. A
    backslash is an ordinary name character (systemd's `\x2d` escapes), never a separator.
    `list_dir` may also list the parents of allowed paths, showing only entries that lead to
    one.
  - Tools never follow a symlink, including a symlinked parent: `read_file` returns
    `is_symlink` with the raw target, and `list_dir` shows links with their targets.
- `sickbay tool SNAPSHOT NAME [--args JSON]` prints a tool result. Exit 0 for any result
  (including an error result), 2 for an unknown tool or `--args` that is not a JSON object,
  3 for an unreadable or corrupt snapshot.
- `HostView.kind(path)` (file, dir, symlink or other, without following links);
  `Snapshot` and `LiveSource` implement it. `HostView.command` raises `UnknownCommandError`
  (a `KeyError`) for an unknown command id, and tools report only that as `unavailable`.

### Changed

- Snapshot reads fail closed: before `read_file`, `command` or `journal` returns bytes, every
  on-disk path component must be a real directory or regular file (no symlink, no Windows
  reparse point), the resolved file must stay under the snapshot root, and the bytes must
  match the manifest `size` and `sha256` (the journal also its entry count). Any failure
  raises `SnapshotError`. `manifest.json` itself goes through the same no-link check (it
  has no hash). A manifest is rejected when a symlink or file path is also a directory, a
  path is both file and symlink, or a field has the wrong type (`sha256`, `size`, the
  journal `entries`, a command's `argv` and `exit_code`, a symlink `target`). A journal
  line that is not a JSON object is an error. `verify` is unchanged.
- The test fixture `tests/fixtures/basic` gained df rows, cgroup files, a full limits table
  and journal priority/unit/pid fields for the tool tests.

### Fixed

- `systemctl show` over every loaded unit failed on the root slice `-.slice`; units now follow `--`.
- Scrubbing: usernames match case-insensitively, `_` and `-` are word boundaries, each GECOS word
  is scrubbed, dash-form MACs and the FQDN domain part are handled, and replacement addresses
  never collide with real documentation-range addresses.
