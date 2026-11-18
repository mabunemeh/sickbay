"""The keyword baseline: a fixed table of journal signatures, no model.

Calls, always the same and in this order: journal_query once per rule (contains = the
rule's signature, limit = QUERY_LIMIT, default newest-first order), then list_units once.
The first rule (in table order) whose query returns an entry wins; its first returned
entry gives the evidence quote and the component. The component is that row's `unit`
field, which journal_query fills from the first of UNIT, USER_UNIT, OBJECT_SYSTEMD_UNIT,
COREDUMP_UNIT, _SYSTEMD_UNIT, _SYSTEMD_USER_UNIT; with none, the leading
`<name>.<unit type>:` token of the message. No hit means nothing_wrong.

This table was written with the seed incidents in view, so its score on them is an upper
bound, not an estimate of how it does on unseen hosts.
"""

from __future__ import annotations

import re
from typing import Any

from sickbay.evals.diagnosis import NOTHING_WRONG, DiagnosisError, validate_diagnosis
from sickbay.evals.predictor import Prediction, empty_usage
from sickbay.evals.trace import Recorder
from sickbay.snapshot.hostview import HostView

# (journal MESSAGE substring, category), checked in this order.
RULES: tuple[tuple[str, str], ...] = (
    ("Too many open files", "fd_limit"),
    ("Failed with result 'oom-kill'", "memory_limit"),
    ("No space left on device", "disk_full"),
    ("Address already in use", "port_conflict"),
    ("Temporary failure in name resolution", "dns"),
    ("certificate has expired", "tls_cert"),
    ("Scheduled restart job", "crash_loop"),
)

FIXES: dict[str, str] = {
    "fd_limit": "Raise the open-file limit that was hit, or reduce the number of files "
    "the process keeps open.",
    "memory_limit": "Raise the memory limit that was hit, or reduce the workload's memory use.",
    "disk_full": "Free space on the full filesystem.",
    "port_conflict": "Free the port that is already in use, or move one service to another port.",
    "dns": "Fix name resolution.",
    "tls_cert": "Renew the expired certificate.",
    "crash_loop": "Fix the cause of the failing start.",
    NOTHING_WRONG: "No action: none of the known failure signatures was found.",
}

QUERY_LIMIT = 5
QUOTE_CHARS = 200
CONFIDENCE = 0.5
_UNIT_TOKEN = re.compile(
    r"^([A-Za-z0-9:_.@\\-]+\.(?:service|socket|slice|scope|mount|target|timer|path|device"
    r"|swap|automount)):"
)


def _component(entry: dict[str, Any]) -> str:
    """The entry's unit field, else the leading '<name>.<unit type>:' token of the message."""
    unit = entry.get("unit")
    if isinstance(unit, str) and unit:
        return unit
    match = _UNIT_TOKEN.match(str(entry.get("message", "")))
    return match.group(1) if match else ""


class KeywordPredictor:
    name = "keyword"
    model: str | None = None
    effort: str | None = None
    prompt_version: str | None = None

    def predict(self, view: HostView) -> Prediction:
        recorder = Recorder(view)
        hits = []
        for signature, category in RULES:
            call_id, result = recorder.call(
                "journal_query", {"contains": signature, "limit": QUERY_LIMIT}
            )
            entries = result.get("entries")
            if isinstance(entries, list) and entries:
                hits.append((category, call_id, entries[0]))
        recorder.call("list_units", {})
        if hits:
            category, call_id, entry = hits[0]
            component = _component(entry)
            evidence = [{"call_id": call_id, "quote": str(entry.get("message", ""))[:QUOTE_CHARS]}]
        else:
            category, component, evidence = NOTHING_WRONG, "", []
        output = {
            "root_cause_category": category,
            "component": component,
            "evidence": evidence,
            "fix": FIXES[category],
            "confidence": CONFIDENCE,
        }
        try:
            diagnosis = validate_diagnosis(output)
        except DiagnosisError:  # e.g. a hit whose entry names no unit
            return Prediction("invalid_output", None, recorder.trace, empty_usage())
        return Prediction("ok", diagnosis, recorder.trace, empty_usage())
