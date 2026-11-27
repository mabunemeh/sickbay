"""Replay a result's recording and check it reproduces the result byte for byte.

The only field allowed to differ is run.created_at; the exit code is 5 when the run hit
its cap (replay enforces the recorded max_usd), else 0. Every committed
results/<name>.json + results/<name>.recording.json pair goes through this.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from sickbay.agent.providers import load_recording, recording_path
from sickbay.cli import main

_CREATED = re.compile(rb'"created_at": "[^"]*"')


def without_created_at(data: bytes) -> bytes:
    return _CREATED.sub(b'"created_at": ""', data)


def assert_replay_reproduces(result: Path, incidents: Path, scratch: Path) -> None:
    recording = recording_path(result)
    assert load_recording(recording)["result_file"] == result.name
    out = scratch / f"replayed-{result.name}"
    argv = ["eval", "--replay", str(recording), "--incidents", str(incidents), "--out", str(out)]
    cases = json.loads(result.read_bytes())["cases"]
    capped = any(c["outcome"] == "budget_exceeded" for c in cases)
    assert main(argv) == (5 if capped else 0)
    assert without_created_at(out.read_bytes()) == without_created_at(result.read_bytes())


def committed_results(results_dir: Path) -> list[Path]:
    """Result files under results_dir that have a recording beside them."""
    return sorted(
        p
        for p in results_dir.glob("*.json")
        if not p.name.endswith(".recording.json") and recording_path(p).is_file()
    )
