"""The tool list sent to the API: the registry's definitions, in order, as strict tools.

Strict tools reject some JSON Schema keywords, so they are removed from a DEEP COPY of each
input_schema, at any depth. The registry is never mutated, and call_tool still validates
the bounds, so an out-of-range argument comes back as an error result the model can read.
"""

from __future__ import annotations

import copy
from typing import Any

from sickbay.tools import tool_definitions

UNSUPPORTED_KEYWORDS = frozenset(
    {"minimum", "maximum", "minLength", "maxLength", "pattern", "multipleOf"}
)
# Keywords whose value maps NAMES to subschemas (the names are not keywords).
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
# Keywords whose value is one subschema or a list of them.
_SCHEMA_VALUES = ("items", "additionalProperties", "not", "anyOf", "allOf", "oneOf", "prefixItems")


def strip_unsupported(schema: Any) -> Any:
    """A copy of schema without UNSUPPORTED_KEYWORDS in any (sub)schema."""
    if isinstance(schema, list):
        return [strip_unsupported(s) for s in schema]
    if not isinstance(schema, dict):
        return copy.deepcopy(schema)
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in UNSUPPORTED_KEYWORDS:
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: strip_unsupported(sub) for name, sub in value.items()}
        elif key in _SCHEMA_VALUES:
            out[key] = strip_unsupported(value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def api_tools() -> list[dict[str, Any]]:
    """Every tool, in tool_definitions() order, with strict: true and a stripped schema."""
    return [
        {
            "name": t["name"],
            "description": t["description"],
            "input_schema": strip_unsupported(t["input_schema"]),
            "strict": True,
        }
        for t in tool_definitions()
    ]
