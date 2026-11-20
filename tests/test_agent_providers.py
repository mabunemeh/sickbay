"""Recording and replay providers, and the recording file format (no SDK needed)."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from agentfakes import FakeProvider, final

from sickbay.agent.providers import (
    LATENCY_KEY,
    RecordingError,
    RecordingProvider,
    ReplayMismatch,
    ReplayProvider,
    canonical_json,
    dumps_recording,
    load_recording,
    recording_path,
    request_sha256,
)
from sickbay.agent.providers.replay import HEADER_KEYS

REPO = Path(__file__).resolve().parent.parent
REQUEST: dict[str, Any] = {
    "model": "m",
    "messages": [{"role": "user", "content": "caf" + chr(233)}],
}


def test_canonical_json_and_hash() -> None:
    text = canonical_json({"b": 1, "a": [chr(233), {"d": None, "c": 2}]})
    assert text == '{"a":["' + chr(233) + '",{"c":2,"d":null}],"b":1}'
    digest = hashlib.sha256(canonical_json(REQUEST).encode("utf-8")).hexdigest()
    assert request_sha256(REQUEST) == digest
    assert request_sha256({"messages": REQUEST["messages"], "model": "m"}) == digest


def recorded(responses: int = 2) -> RecordingProvider:
    recorder = RecordingProvider(FakeProvider([final()] * responses, latency=1.234))
    recorder.start_case("a-case", 0)
    for i in range(responses):
        out = recorder.complete({**REQUEST, "n": i})
        assert out[LATENCY_KEY] == 1.234
    return recorder


def test_recording_groups_exchanges_per_case() -> None:
    recorder = recorded()
    recorder.start_case("a-case", 1)
    assert recorder.name == "fake"
    (first, second) = recorder.cases
    assert (first["incident"], first["repeat"], second["repeat"]) == ("a-case", 0, 1)
    assert second["exchanges"] == []
    exchange = first["exchanges"][0]
    assert exchange == {
        "request_sha256": request_sha256({**REQUEST, "n": 0}),
        "response": final(),
        "latency_s": 1.234,
    }


def test_recording_is_a_copy_of_the_response() -> None:
    recorder = RecordingProvider(FakeProvider([final()]))
    recorder.start_case("a", 0)
    out = recorder.complete(REQUEST)
    out["content"].append({"type": "text", "text": "mutated later"})
    assert recorder.cases[0]["exchanges"][0]["response"] == final()


def test_recording_needs_a_case() -> None:
    with pytest.raises(RuntimeError):
        RecordingProvider(FakeProvider([final()])).complete(REQUEST)


def test_document_header() -> None:
    header = dict.fromkeys(HEADER_KEYS, "x")
    doc = recorded().document(**header)
    assert doc["recording_schema"] == 1 and len(doc["cases"]) == 1
    with pytest.raises(ValueError):
        recorded().document(model="m")


def test_replay_returns_recorded_responses_in_order() -> None:
    replay = ReplayProvider(recorded().cases)
    replay.start_case("a-case", 0)
    for i in range(2):
        out = replay.complete({**REQUEST, "n": i})
        assert out.pop(LATENCY_KEY) == 1.234 and out == final()
    replay.finish()
    assert replay.name == "replay"


def test_replay_hands_back_copies() -> None:
    cases = recorded(1).cases
    replay = ReplayProvider(cases)
    replay.start_case("a-case", 0)
    replay.complete({**REQUEST, "n": 0})["content"].clear()
    assert cases[0]["exchanges"][0]["response"] == final()


def test_replay_mismatch_names_incident_repeat_and_index() -> None:
    replay = ReplayProvider(recorded().cases)
    replay.start_case("a-case", 0)
    replay.complete({**REQUEST, "n": 0})
    with pytest.raises(ReplayMismatch, match="incident a-case repeat 0 exchange 1: the request"):
        replay.complete({**REQUEST, "n": 99})


def test_replay_runs_out() -> None:
    replay = ReplayProvider(recorded(1).cases)
    replay.start_case("a-case", 0)
    replay.complete({**REQUEST, "n": 0})
    with pytest.raises(ReplayMismatch, match="exchange 1: the recording has no more exchanges"):
        replay.complete({**REQUEST, "n": 1})


def test_replay_unused_exchanges_and_cases_are_mismatches() -> None:
    replay = ReplayProvider(recorded().cases)
    replay.start_case("a-case", 0)
    replay.complete({**REQUEST, "n": 0})
    with pytest.raises(ReplayMismatch, match="exchange 1: the run stopped before"):
        replay.finish()
    replay = ReplayProvider(recorded().cases)
    with pytest.raises(ReplayMismatch, match="recorded but was not run"):
        replay.finish()
    replay = ReplayProvider(recorded().cases)
    with pytest.raises(ReplayMismatch, match="not in the recording"):
        replay.start_case("b-case", 0)
    with pytest.raises(ReplayMismatch, match="before any case"):
        ReplayProvider([]).complete(REQUEST)


def _write(tmp_path: Path, doc: dict[str, Any]) -> Path:
    path = tmp_path / "r.recording.json"
    path.write_bytes(dumps_recording(doc))
    return path


def good_document() -> dict[str, Any]:
    header = {
        "result_file": "r.json",
        "predictor": "agent",
        "provider": "anthropic",
        "model": "claude-haiku-5-5",
        "effort": "medium",
        "prompt_version": "agent-v1",
        "max_usd": 0.5,
        "repeats": 1,
    }
    return recorded().document(**header)


def test_load_recording_round_trip(tmp_path: Path) -> None:
    doc = good_document()
    assert load_recording(_write(tmp_path, doc)) == doc
    data = dumps_recording(doc)
    assert data.endswith(b"}\n") and b"\r" not in data and b'\n  "cases": [' in data


@pytest.mark.parametrize(
    "change",
    [
        {"recording_schema": 2},
        {"extra": 1},
        {"model": None},
        {"repeats": 0},
        {"repeats": True},
        {"max_usd": "5"},
        {"cases": {}},
        {"cases": [{"incident": "a", "repeat": 0}]},
        {"cases": [{"incident": "a", "repeat": 0, "exchanges": [{"response": {}}]}]},
    ],
)
def test_load_recording_rejects_bad_shapes(tmp_path: Path, change: dict[str, Any]) -> None:
    with pytest.raises(RecordingError):
        load_recording(_write(tmp_path, {**good_document(), **change}))


def test_load_recording_rejects_missing_keys_and_bad_files(tmp_path: Path) -> None:
    doc = good_document()
    del doc["max_usd"]
    with pytest.raises(RecordingError):
        load_recording(_write(tmp_path, doc))
    with pytest.raises(RecordingError):
        load_recording(tmp_path / "missing.recording.json")
    (tmp_path / "bin.recording.json").write_bytes(b"\xff\xfe")
    with pytest.raises(RecordingError):
        load_recording(tmp_path / "bin.recording.json")


def test_recording_path() -> None:
    assert recording_path(Path("results/agent-haiku.json")) == Path(
        "results/agent-haiku.recording.json"
    )
    assert recording_path(Path("out")) == Path("out.recording.json")


def test_replay_and_cli_import_without_the_sdk(tmp_path: Path) -> None:
    """A fake run is recorded, then replayed in a process where `import anthropic` fails."""
    code = (
        "import sys\n"
        "sys.modules['anthropic'] = None\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "from pathlib import Path\n"
        "from agentfakes import FakeProvider, final, tool_turn, tool_use\n"
        "from evalfakes import label_yaml\n"
        "import shutil\n"
        "from sickbay import cli\n"
        "root = Path(sys.argv[1])\n"
        "shutil.copytree(Path(sys.argv[2]) / 'fixtures' / 'basic', root / 'i' / 'a' / 'snapshot')\n"
        "(root / 'i' / 'a' / 'label.yaml').write_text(label_yaml('a'))\n"
        "turns = [tool_turn(tool_use('t', 'disk_usage', {})), final()]\n"
        "real = cli._make_provider\n"
        "cli._make_provider = lambda name: FakeProvider(turns)\n"
        "base = ['eval', '--incidents', str(root / 'i')]\n"
        "assert cli.main([*base, '--agent', '--model', 'claude-haiku-5-5', '--max-usd', '1',\n"
        "                 '--out', str(root / 'r.json')]) == 0\n"
        "assert cli.main([*base, '--replay', str(root / 'r.recording.json'),\n"
        "                 '--out', str(root / 'again.json')]) == 0\n"
        "cli._make_provider = real\n"
        "assert cli.main([*base, '--agent', '--model', 'claude-haiku-5-5', '--max-usd', '1',\n"
        "                 '--out', str(root / 'x.json')]) == 2\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), str(REPO / "tests")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"
    assert "pip install 'sickbay[anthropic]'" in done.stderr


def test_leftover_exchange_in_a_non_final_case_is_a_mismatch() -> None:
    recorder = recorded()  # a-case repeat 0 has two exchanges
    recorder.start_case("a-case", 1)
    replay = ReplayProvider(recorder.cases)
    replay.start_case("a-case", 0)
    replay.complete({**REQUEST, "n": 0})
    with pytest.raises(
        ReplayMismatch, match="incident a-case repeat 0 exchange 1: the run stopped"
    ):
        replay.start_case("a-case", 1)
