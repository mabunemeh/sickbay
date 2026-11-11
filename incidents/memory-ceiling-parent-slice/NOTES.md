# memory-ceiling-parent-slice

Simulates a service with no memory limit of its own, started inside `batch.slice` which has `MemoryMax=64M` and `MemorySwapMax=0`. The service wants about 200 MB and is killed by the kernel; the host itself has plenty of free memory because the ceiling is on the ancestor slice.

Unit: `report-builder.service in batch.slice`. Runtime files: `/run/report-svc, /run/systemd/system/batch.slice` (all under `/run`, gone after a reboot).

## Run it

Needs a systemd host (WSL2 with systemd, or a VM) and passwordless sudo.

```
scripts/reproduce.sh memory-ceiling-parent-slice /tmp/snap-memory-ceiling-parent-slice                      # inject, capture, clean up
scripts/reproduce.sh memory-ceiling-parent-slice /tmp/snap-memory-ceiling-parent-slice --update-committed   # also replace snapshot/
sudo incidents/memory-ceiling-parent-slice/inject.sh    # by hand; blocks until the condition holds
sudo incidents/memory-ceiling-parent-slice/cleanup.sh   # idempotent
```

`inject.sh` refuses unless root and unless none of its units or files exist, acts only
through `systemd-run` transient units and files in `/run`, and cleans up after itself if it
fails.

## Label notes

- Component is `batch.slice`, the unit whose limit bit. `report-builder.service` (the unit that
  was killed) is the alternative; Mohammad to decide which the benchmark should expect.
- There is no `command:free` evidence item: `free -b` output is all numbers that change on every
  capture and has no stable substring that proves memory was plentiful. The captured
  `/proc/meminfo` and `free` show it, but the label does not assert it.

## Status

`label.yaml` is a DRAFT awaiting review by Mohammad. The label schema is not final
(`label_schema: 0`). Do not treat the snapshot as a benchmark input until the label is reviewed.
