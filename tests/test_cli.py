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


def test_tool_prints_result_json(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert main(["tool", str(FIXTURE), "read_file", "--args", '{"path": "/proc/loadavg"}']) == 0
    out = capsysbinary.readouterr().out
    assert out.endswith(b"\n") and json.loads(out)["path"] == "/proc/loadavg"


def test_tool_error_result_exits_0(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert main(["tool", str(FIXTURE), "read_file", "--args", '{"path": "/etc/shadow"}']) == 0
    assert json.loads(capsysbinary.readouterr().out)["error"]["kind"] == "not_allowed"
    assert main(["tool", str(FIXTURE), "list_units", "--args", '{"limit": 0}']) == 0
    assert json.loads(capsysbinary.readouterr().out)["error"]["kind"] == "invalid_argument"


def test_tool_default_args_and_utf8(capsysbinary: pytest.CaptureFixture[bytes]) -> None:
    assert main(["tool", str(FIXTURE), "journal_query"]) == 0
    assert "started é ok".encode() in capsysbinary.readouterr().out


@pytest.mark.parametrize("bad", ["[]", "1", "null", "{", '"x"'])
def test_tool_args_not_an_object_exits_2(bad: str) -> None:
    assert main(["tool", str(FIXTURE), "list_units", "--args", bad]) == 2


def test_tool_unknown_name_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tool", str(FIXTURE), "dns_check"]) == 2
    assert "unknown tool" in capsys.readouterr().err


def test_tool_unreadable_or_corrupt_snapshot_exits_3(tmp_path: Path) -> None:
    assert main(["tool", str(tmp_path / "nope"), "list_units"]) == 3
    shutil.copytree(FIXTURE, tmp_path / "s")
    (tmp_path / "s" / "commands" / "systemctl-list-units.txt").write_bytes(b"tampered")
    assert main(["tool", str(tmp_path / "s"), "list_units"]) == 3


def test_tool_symlinked_manifest_exits_3(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "s")
    manifest = tmp_path / "s" / "manifest.json"
    (tmp_path / "m").write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(tmp_path / "m")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    assert main(["tool", str(tmp_path / "s"), "list_units"]) == 3
    assert main(["verify", str(tmp_path / "s")]) == 3


def test_tool_malformed_manifest_exits_3(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE, tmp_path / "s")
    p = tmp_path / "s" / "manifest.json"
    m = json.loads(p.read_text())
    for c in m["commands"]:
        c.pop("exit_code")
    p.write_text(json.dumps(m))
    assert main(["tool", str(tmp_path / "s"), "list_units"]) == 3
