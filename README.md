# sickbay

Capture a frozen, scrubbed evidence snapshot of a Linux systemd host, and read
it back through the same interface a live host would give.

This is the first piece of a benchmark of sick Linux hosts. Today the repo has
the snapshot format, `capture`, `verify`, the reader, ten read-only tools over
snapshots, and three seed incidents (`incidents/`, labels still drafts). Scorers and
agents are not built yet.
Related, different work: SREBattle (live VMs, pass/fail), ITBench-AA and SREGym
(Kubernetes).

```
pip install -e ".[dev]"
sudo sickbay capture ./snap --since=-1h
sickbay verify ./snap
sickbay tool ./snap read_file --args '{"path": "/proc/meminfo"}'
```

`capture` reads `/proc`, `/sys/fs/cgroup`, `/etc` and `/run/systemd`, runs a
fixed list of commands and takes the journal. Hostnames, users, IPs, MACs, the
machine id and `Environment=` values are scrubbed unless `--no-scrub` is given.
Use `--since=-1h` (with the equals sign) for relative times.

`tool` runs one allowlisted, size-capped tool (`list_units`, `unit_status`,
`journal_query`, `process_list`, `process_limits`, `cgroup_tree`, `disk_usage`,
`sockets`, `read_file`, `list_dir`) and prints its JSON result. Tools never follow symlinks.

Exit codes: 0 ok (for `tool`, any result, including an error result), 2 usage, a
bad/unwritable capture DEST, an unknown tool or `--args` that is not a JSON object, 3
snapshot unreadable, corrupt or failing verification. `scripts/reproduce.sh <id> <dest>`
re-creates an incident (WSL/VM with systemd and sudo) and captures it.

Licensed under Apache-2.0.
