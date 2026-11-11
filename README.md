# sickbay

Capture a frozen, scrubbed evidence snapshot of a Linux systemd host, and read
it back through the same interface a live host would give.

This is the first piece of a benchmark of sick Linux hosts. Today the repo has
the snapshot format, `capture`, `verify`, the reader, and three seed incidents
(`incidents/`, labels still drafts). Tools, scorers and agents are not built yet.
Related, different work: SREBattle (live VMs, pass/fail), ITBench-AA and SREGym
(Kubernetes).

```
pip install -e ".[dev]"
sudo sickbay capture ./snap --since=-1h
sickbay verify ./snap
```

`capture` reads `/proc`, `/sys/fs/cgroup`, `/etc` and `/run/systemd`, runs a
fixed list of commands and takes the journal. Hostnames, users, IPs, MACs, the
machine id and `Environment=` values are scrubbed unless `--no-scrub` is given.
Use `--since=-1h` (with the equals sign) for relative times.

Exit codes: 0 ok, 2 usage or a bad/unwritable capture DEST, 3 snapshot unreadable or
failing verification. `scripts/reproduce.sh <id> <dest>` re-creates an incident (WSL/VM with
systemd and sudo) and captures it.

Licensed under Apache-2.0.
