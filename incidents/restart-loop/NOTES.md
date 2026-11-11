# restart-loop

Simulates a service whose settings file is missing, so it exits 1 on every start while `Restart=always`, `RestartSec=2` and `StartLimitIntervalSec=0` make systemd relaunch it forever.

Unit: `quote-sync.service`. Runtime files: `/run/quote-sync` (all under `/run`, gone after a reboot).

## Run it

Needs a systemd host (WSL2 with systemd, or a VM) and passwordless sudo.

```
scripts/reproduce.sh restart-loop /tmp/snap-restart-loop                      # inject, capture, clean up
scripts/reproduce.sh restart-loop /tmp/snap-restart-loop --update-committed   # also replace snapshot/
sudo incidents/restart-loop/inject.sh    # by hand; blocks until the condition holds
sudo incidents/restart-loop/cleanup.sh   # idempotent
```

`inject.sh` refuses unless root and unless none of its units or files exist, acts only
through `systemd-run` transient units and files in `/run`, and cleans up after itself if it
fails.

## Status

`label.yaml` is a DRAFT awaiting review by Mohammad. The label schema is not final
(`label_schema: 0`). Do not treat the snapshot as a benchmark input until the label is reviewed.
