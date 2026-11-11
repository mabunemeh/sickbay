# fd-exhaustion

Simulates a service that opens spool files and never closes them until it hits its `LimitNOFILE=512`, then keeps running while every `open()` fails with `[Errno 24] Too many open files`.

Unit: `ledger-api.service`. Runtime files: `/run/ledger-svc, /run/ledger-spool` (all under `/run`, gone after a reboot).

## Run it

Needs a systemd host (WSL2 with systemd, or a VM) and passwordless sudo.

```
scripts/reproduce.sh fd-exhaustion /tmp/snap-fd-exhaustion                      # inject, capture, clean up
scripts/reproduce.sh fd-exhaustion /tmp/snap-fd-exhaustion --update-committed   # also replace snapshot/
sudo incidents/fd-exhaustion/inject.sh    # by hand; blocks until the condition holds
sudo incidents/fd-exhaustion/cleanup.sh   # idempotent
```

`inject.sh` refuses unless root and unless none of its units or files exist, acts only
through `systemd-run` transient units and files in `/run`, and cleans up after itself if it
fails.

## Status

`label.yaml` is a DRAFT awaiting review by Mohammad. The label schema is not final
(`label_schema: 0`). Do not treat the snapshot as a benchmark input until the label is reviewed.
