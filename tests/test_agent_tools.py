"""The strict tool list and the packaged prompts."""

from __future__ import annotations

import copy
from importlib import resources
from pathlib import Path
from typing import Any

from sickbay.agent.loop import load_prompt
from sickbay.agent.tools import UNSUPPORTED_KEYWORDS, api_tools, strip_unsupported
from sickbay.evals.baselines.dump import prompt_text
from sickbay.tools import TOOLS, tool_definitions

REPO = Path(__file__).resolve().parent.parent


def keywords(schema: Any) -> set[str]:
    """Every keyword used in a schema, at any depth (property NAMES are not keywords)."""
    found: set[str] = set()
    if isinstance(schema, list):
        for sub in schema:
            found |= keywords(sub)
    elif isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            if key == "properties":
                for sub in value.values():
                    found |= keywords(sub)
            elif isinstance(value, dict | list):
                found |= keywords(value)
    return found


def test_api_tools_are_strict_and_stripped() -> None:
    before = copy.deepcopy(tool_definitions())
    tools = api_tools()
    assert [t["name"] for t in tools] == list(TOOLS)
    for tool, original in zip(tools, before, strict=True):
        assert set(tool) == {"name", "description", "input_schema", "strict"}
        assert tool["strict"] is True
        assert tool["description"] == original["description"]
        assert not keywords(tool["input_schema"]) & UNSUPPORTED_KEYWORDS
        assert (
            tool["input_schema"]["properties"].keys()
            == original["input_schema"]["properties"].keys()
        )
    # The registry is not mutated: its schemas still hold the bounds call_tool enforces.
    assert tool_definitions() == before
    assert TOOLS["list_units"].input_schema["properties"]["limit"]["minimum"] == 1
    assert "pattern" in TOOLS["unit_status"].input_schema["properties"]["unit"]
    assert any("minimum" in keywords(t["input_schema"]) for t in tool_definitions())


def test_strip_keeps_properties_named_like_keywords() -> None:
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "pattern": "^a$", "maxLength": 3},
            "minimum": {"type": "array", "items": {"type": "integer", "minimum": 0}},
            "nested": {
                "type": "object",
                "properties": {"x": {"type": "number", "multipleOf": 2, "maximum": 9}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
    original = copy.deepcopy(schema)
    out = strip_unsupported(schema)
    assert schema == original
    assert out == {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "minimum": {"type": "array", "items": {"type": "integer"}},
            "nested": {
                "type": "object",
                "properties": {"x": {"type": "number"}},
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }
    assert not keywords(out) & UNSUPPORTED_KEYWORDS


def test_api_tools_are_byte_stable() -> None:
    assert api_tools() == api_tools()


def test_prompts_are_package_data() -> None:
    """Read through importlib.resources (no install step), LF only."""
    dump = resources.files("sickbay.evals.baselines").joinpath("prompts", "dump_v1.txt")
    agent = resources.files("sickbay.agent").joinpath("prompts", "agent_v1.txt")
    for resource in (dump, agent):
        data = resource.read_bytes()
        assert b"\r" not in data and data.endswith(b"\n")
        data.decode("utf-8")
    assert prompt_text() == dump.read_bytes().decode("utf-8")
    system, task = load_prompt()
    assert "UNTRUSTED DATA" in system and "read-only" in system
    assert "c1, c2" in system and "at least 8" in system
    assert "[user]" not in system and "[system]" not in task and task.strip()


def test_hatch_packages_the_whole_source_tree() -> None:
    """hatchling's `packages = ["src/sickbay"]` ships every file under the package,
    *.txt included, unless an exclude says otherwise."""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/sickbay"]' in text
    assert "exclude" not in text.split("[tool.hatch.build", 1)[1].split("\n[tool.ruff", 1)[0]
    assert (REPO / "src" / "sickbay" / "agent" / "prompts" / "agent_v1.txt").is_file()


def test_strict_copy_is_deep() -> None:
    before = copy.deepcopy(tool_definitions())
    tools = {t["name"]: t for t in api_tools()}
    tools["list_units"]["input_schema"]["properties"]["state"]["enum"].append("mutated")
    tools["list_units"]["input_schema"]["properties"]["limit"]["type"] = "mutated"
    tools["unit_status"]["input_schema"]["required"].append("mutated")
    assert tool_definitions() == before
    assert "mutated" not in TOOLS["list_units"].input_schema["properties"]["state"]["enum"]
