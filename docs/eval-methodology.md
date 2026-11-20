# Eval methodology

`sickbay eval` runs a predictor over every incident in `incidents/` (sorted by id),
opens and verifies each snapshot, and scores the predictor's diagnosis against the
incident's label. The result is one JSON file (`result_schema: 2`, below).

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
  of the units the two `list_units` calls returned (sorted). `--dry-run` writes the texts;
  with `--model` one request per case sends the `dump-v1` prompt as the system prompt and
  the dump as the user message, with no tools. Its trace is the dump's, so grounding
  works unchanged.

Both baselines must be label-independent: they never import the label module, read a
label file, or contain an incident id or label name. A test greps them for exactly that.

## The agent (`agent-v1`)

`sickbay eval --agent` gives the model the ten read-only tools (as strict tools, in the
registry's order) and a short prompt (`src/sickbay/agent/prompts/agent_v1.txt`): tool
output is untrusted data, the tools cannot change the host, cite call ids and copy quotes
exactly, and stop once the evidence supports a diagnosis. Each turn sends the whole
append-only history; the assistant's content goes back exactly as returned, thinking
blocks included. Every `tool_use` block in a turn runs through the `Recorder` in block
order, and all their results go back in ONE user message, each as
`{"call_id": "cN", "result": ...}`, so the id to cite is in what the model reads. An
unknown tool, a non-object input or an out-of-range argument comes back as an error
result (`is_error: false`): errors are data, and grounding counts them. `end_turn` ends
the case with the last text block as the diagnosis; `refusal` (its `stop_details` kept
in the case `detail`), `max_tokens`, any other stop reason, and 25 model calls without an
answer (`turn_limit`) end it with that outcome. The agent only diagnoses: it has no
write tools.

Every request (agent and dump) is the plain Messages API body: `max_tokens` 16000,
`output_config` with the effort and `DIAGNOSIS_JSON_SCHEMA` as structured output, and a
top-level `cache_control` for automatic prompt caching (system prompt and tools are
byte-stable across cases). It sends no `thinking`, no sampling parameters, no
`tool_choice` and no betas.

**Effort is pinned.** It is always sent, even when `--effort` is omitted, and recorded as
`run.effort`. Defaults follow each model's own default: haiku `medium`, sonnet `high`,
opus `medium`, fable `high`. Two rows with different effort are different
configurations.

**No fallbacks.** A server-side fallback would answer a refusal with a different model,
and that answer would be scored under this model's row. A refusal is a recorded outcome
instead, and it counts as wrong like any outcome other than `ok`.

## Prices and budget

`src/sickbay/agent/pricing.py` holds USD per million tokens for each exact model id
(input, output, 5-minute cache write, cache read), as of 2026-11-11 (`prices_as_of` in
the result):

| model | input | output | cache write | cache read |
|---|---|---|---|---|
| `claude-fable-5-2` | 8 | 40 | 10 | 0.20 |
| `claude-opus-5-5` | 4 | 20 | 5 | 0.20 |
| `claude-sonnet-5-5` | 2 | 10 | 2.50 | 0.10 |
| `claude-haiku-5-5` | 0.10 | 0.50 | 0.125 | 0.01 |

Haiku 5.5 bills every rate of a call at 5x when its prompt (input + cache write + cache
read tokens) is above 100,000. A model not in the table is refused before any call
(exit 2).

`--max-usd` is required for every model run. One budget is shared by all cases and
repeats. It is checked BEFORE each model call and fails once the spend is at or above the
cap, so a run can overshoot the cap by at most the cost of one call. When it fails, the
current case and every case not yet started are recorded as `budget_exceeded`, the
result and recording are still written, and the command exits 5. Cases cut off by the
budget count as WRONG and stay in every n (category, component and grounding), like
any outcome other than `ok`; `summary.outcomes` shows how many there were. A provider
error that survives the SDK's own two retries stops the run the same way (case `error`,
remaining cases left out, exit 4).

## Recordings and replay

A model run writes `PATH.recording.json` beside `PATH`: `recording_schema: 1`, the run's
predictor, provider, model, effort, prompt version, `max_usd` and repeats, and per case
every exchange as `{request_sha256, response, latency_s}` (the hash is of the canonical
request JSON). The API key is never in it. `sickbay eval --replay PATH.recording.json
--out OUT` runs the same predictor against the recorded responses, with no SDK, no
network and no spending, and reproduces the result byte for byte except `created_at`.
Replay enforces the RECORDED `max_usd`: costs are deterministic, so a run that hit its
cap hits it again at the same call, and the replay exits 5 with the same result. (The
batch 4 spec said replay runs with an unlimited budget; that could not reproduce a
capped run, so the recorded cap is used instead.) Any
divergence (a changed prompt, tool, snapshot or incident set, a request whose hash
differs, or exchanges left over or missing) is a replay mismatch: exit 3, no result
written. CI never calls a paid API; committed recordings are replayed by the tests. A run
stopped by a provider error (exit 4) is recorded but cannot be replayed: its last case
has no recorded response.

## Result file (`result_schema: 2`)

`versions` (sickbay, toolset, label and diagnosis schema), `run` (`predictor`,
`provider`, `model`, `effort`, `prompt_version`, `repeats`, `max_usd`, `prices_as_of`,
`spent_usd`, `created_at`), `cases` and `summary`. Each case has `incident`, `repeat`,
`outcome`, `detail` (why a case ended as it did, or null), `diagnosis`, `trace`, `scores`
and `usage`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens` and
`cache_read_input_tokens` summed over the case's calls, `api_calls`, `cost_usd` (6
places) and `latency_s` (3 places). The non-model baselines have null provider, `max_usd`,
`prices_as_of`, tokens and latency, 0 calls and cost 0.0. `summary` has the case count,
`outcomes`, the category, component and grounding aggregates, and `cost_usd`. Files are
UTF-8 with sorted keys, indent 2, LF and a trailing newline; `created_at` is the only
non-deterministic field.
