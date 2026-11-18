# Eval methodology

`sickbay eval` runs a predictor over every incident in `incidents/` (sorted by id),
opens and verifies each snapshot, and scores the predictor's diagnosis against the
incident's label. The result is one JSON file (`result_schema: 1`).

## Labels (`label_schema: 1`)

`incidents/<id>/label.yaml` holds the ground truth: `root_cause_category` (one of the
frozen categories `fd_limit`, `memory_limit`, `crash_loop`, `disk_full`, `dns`,
`tls_cert`, `port_conflict`, `config_error`, `nothing_wrong`), `component` (null only
for `nothing_wrong`), `also_accepted` (other components scored correct), `split` (`dev`
or `heldout`; heldout incidents are skipped), `injection`, `status` (`draft` or
`reviewed`), and the evidence, leak-string and name lists the snapshot tests use.
Unknown keys are an error. All three seed labels are still `draft`.

## Diagnosis (`diagnosis_schema: 1`)

A predictor answers `{root_cause_category, component, evidence: [{call_id, quote}], fix,
confidence}`. Anything that fails validation (wrong keys or types, unknown category,
confidence outside [0, 1] or a bool, NUL or invalid UTF-8) is the case outcome
`invalid_output`. Any case whose outcome is not `ok` counts as wrong: category and
component false, grounding 0.0 (`no_diagnosis`). `summary.outcomes` counts each outcome.

## Scorers

- **category**: exact match with the label.
- **component**: both sides normalised, then compared case-sensitively with the label's
  `component` and each `also_accepted`. Normalising strips surrounding ASCII whitespace
  and appends `.service` to a value with no `/` and no unit-type suffix (`.service
  .socket .slice .scope .mount .target .timer .path .device .swap .automount`). Nothing
  else: no lowercasing, no pid or path resolution. `component_matched` records
  `primary`, `also_accepted` or null. For a `nothing_wrong` label, component is correct
  iff the diagnosis is also `nothing_wrong`. For any other label, a `nothing_wrong`
  diagnosis never earns component credit, whatever unit it names.
- **grounding**: an evidence item is grounded iff its `call_id` is in the trace, and its
  quote, stripped, is at least 8 characters and a substring of a decoded leaf of that
  call's result: a string as it is, a number via `str()`. Keys, bools and nulls are not
  leaves. Matching never uses serialised JSON, where escaping would turn `"` into `\"`.
  An error result is still a call. The case score is grounded / total; no evidence
  scores 0.0 (`no_evidence`).
- **fix**: null (`not_scored`) until an LLM judge exists (batch 6).
- **safety**: null unless the label is a prompt-injection case; none exist yet (batch 5).

A null score is never counted as 0 or 1: aggregates use the non-null values and record
their `n`.

## Baselines

Every predictor, including the baselines, calls tools through a `Recorder`, so caps and
the allowlist apply and every call lands in the trace as `c1`, `c2`, ...

- **keyword**: one `journal_query` per signature in a fixed table, then `list_units`.
  The first rule in table order with a hit gives the category; its first returned entry
  gives the one evidence quote (the message, first 200 characters) and the component:
  the row's `unit` field, which `journal_query` fills from the first of `UNIT`,
  `USER_UNIT`, `OBJECT_SYSTEMD_UNIT`, `COREDUMP_UNIT`, `_SYSTEMD_UNIT`,
  `_SYSTEMD_USER_UNIT`, else the leading `<name>.<type>:` token of the message. Fix
  sentences are fixed per category and generic. No hit is `nothing_wrong`. This table was written with the three
  seed incidents in view, so its score on them is an upper bound, not an estimate.
- **dump**: a fixed recipe of calls rendered into one text (capped at 60,000 bytes by
  dropping whole calls from the end) for a model to diagnose in one shot: `list_units`
  for failed and for activating units, the priority 0-4 and the newest journal entries,
  `process_list`, `cgroup_tree`, `disk_usage`, `sockets`, and `unit_status` for up to 5
  of the units the two `list_units` calls returned (sorted). Batch 3 only
  builds it (`--dry-run`); it is scored once a model provider exists (batch 4).

Both baselines must be label-independent: they never import the label module, read a
label file, or contain an incident id or label name. A test greps them for exactly that.
