"""Regenerate tests/fixtures/eval/keyword-golden.json. Run: python tests/make_eval_golden.py"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sickbay.evals.baselines.keyword import KeywordPredictor
from sickbay.evals.runner import dumps_result, run_eval

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tests" / "fixtures" / "eval" / "keyword-golden.json"
CREATED_AT = "<created_at>"


def keyword_result() -> dict[str, Any]:
    """The keyword result on the committed incidents, with created_at pinned."""
    result = run_eval(KeywordPredictor(), REPO / "incidents")
    result["run"]["created_at"] = CREATED_AT
    return result


if __name__ == "__main__":
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_bytes(dumps_result(keyword_result()))
    print(f"wrote {GOLDEN}")
