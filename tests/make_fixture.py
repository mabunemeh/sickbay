"""Regenerate tests/fixtures/basic from the fake host. Run: python tests/make_fixture.py"""

from __future__ import annotations

import shutil
from pathlib import Path

from fakes import FIXED_CLOCK, build_source

from sickbay.snapshot import capture

FIXTURE = Path(__file__).parent / "fixtures" / "basic"

if __name__ == "__main__":
    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    capture(build_source(), FIXTURE, clock=lambda: FIXED_CLOCK)
    print(f"wrote {FIXTURE}")
