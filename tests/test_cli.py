import json
import shutil
import sys
from pathlib import Path

import pytest

from sickbay.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "basic"


def test_verify_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify", str(FIXTURE)]) == 0
    assert capsys.readouterr().out.startswith("ok:")


def test_verify_hash_mismatch_exits_3(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    shutil.copytree(FIXTURE, tmp_path / "s")
    (tmp_path / "s" / "commands" / "df.txt").write_bytes(b"tampered")
    assert main(["verify", str(tmp_path / "s")]) == 3
    assert "mismatch" in capsys.readouterr().err


def test_verify_unreadable_exits_3(tmp_path: Path) -> None:
    assert main(["verify", str(tmp_path / "nope")]) == 3


def test_verify_bad_version_exits_3(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "s")
    p = tmp_path / "s" / "manifest.json"
    m = json.loads(p.read_text())
    m["format_version"] = 99
    p.write_text(json.dumps(m))
    assert main(["verify", str(tmp_path / "s")]) == 3


def test_usage_error_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_capture_rejects_non_empty_dest(tmp_path: Path) -> None:
    (tmp_path / "x").write_text("x")
    assert main(["capture", str(tmp_path)]) == 2


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="live capture is Linux only")
def test_capture_live_smoke(tmp_path: Path) -> None:
    assert main(["capture", str(tmp_path / "snap"), "--since=-1m"]) == 0
    assert main(["verify", str(tmp_path / "snap")]) == 0


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="non-Linux message path")
def test_capture_refused_off_linux(tmp_path: Path) -> None:
    assert main(["capture", str(tmp_path / "snap")]) == 2
