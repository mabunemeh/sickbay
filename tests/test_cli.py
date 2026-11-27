import json
import re
import shutil
import sys
from pathlib import Path

import pytest
from evalfakes import label_yaml

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


# -- eval -------------------------------------------------------------------------------------


def _incidents(tmp_path: Path, label: str | None = None) -> Path:
    root = tmp_path / "incidents"
    shutil.copytree(FIXTURE, root / "a-case" / "snapshot")
    text = label if label is not None else label_yaml("a-case")
    (root / "a-case" / "label.yaml").write_bytes(text.encode("utf-8"))
    return root


def test_eval_keyword_to_stdout_exits_0(
    tmp_path: Path, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    assert main(["eval", "--baseline", "keyword", "--incidents", str(_incidents(tmp_path))]) == 0
    captured = capsysbinary.readouterr()
    result = json.loads(captured.out)
    assert result["result_schema"] == 2 and result["cases"][0]["incident"] == "a-case"
    assert captured.err.startswith(b"keyword: category ")
    assert captured.err.endswith(b"(n=1)\n") or captured.err.endswith(b"(n=1)\r\n")


def test_eval_keyword_to_file_exits_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "r.json"
    args = ["eval", "--baseline", "keyword", "--incidents", str(_incidents(tmp_path))]
    assert main([*args, "--out", str(out)]) == 0
    data = out.read_bytes()
    assert b"\r" not in data and data.endswith(b"}\n")
    assert capsys.readouterr().out == ""


def test_eval_default_incidents_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    _incidents(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert main(["eval", "--baseline", "keyword"]) == 0
    assert json.loads(capsysbinary.readouterr().out)["summary"]["cases"] == 1


def test_eval_usage_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = str(_incidents(tmp_path))
    assert main(["eval", "--baseline", "dump", "--incidents", root]) == 2
    assert "--model is required for a model run" in capsys.readouterr().err
    assert main(["eval", "--baseline", "dump", "--incidents", root, "--dry-run"]) == 2
    assert main(["eval", "--baseline", "keyword", "--incidents", root, "--dry-run"]) == 2
    bad_out = str(tmp_path / "no" / "such" / "dir" / "r.json")
    assert main(["eval", "--baseline", "keyword", "--incidents", root, "--out", bad_out]) == 2
    (tmp_path / "file").write_bytes(b"x")
    dump_args = ["eval", "--baseline", "dump", "--incidents", root, "--dry-run"]
    assert main([*dump_args, "--out", str(tmp_path / "file")]) == 2
    with pytest.raises(SystemExit) as exc:
        main(["eval", "--baseline", "nope"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["eval"])
    assert exc.value.code == 2


def test_eval_dump_dry_run_writes_files(tmp_path: Path) -> None:
    out = tmp_path / "dumps"
    args = ["eval", "--baseline", "dump", "--incidents", str(_incidents(tmp_path))]
    assert main([*args, "--dry-run", "--out", str(out)]) == 0
    text = (out / "a-case.dump.txt").read_bytes()
    assert text.startswith(b"### c1 list_units ") and text.endswith(b"### dump_truncated false\n")
    assert b"\r" not in text


@pytest.mark.parametrize("baseline", [["keyword"], ["dump", "--dry-run", "--out", "OUT"]])
def test_eval_bad_label_or_snapshot_exits_3(tmp_path: Path, baseline: list[str]) -> None:
    extra = [str(tmp_path / "out") if a == "OUT" else a for a in baseline]
    bad_label = _incidents(tmp_path / "1", label_yaml("a-case", label_schema=0))
    assert main(["eval", "--incidents", str(bad_label), "--baseline", *extra]) == 3
    corrupt = _incidents(tmp_path / "2")
    (corrupt / "a-case" / "snapshot" / "commands" / "df.txt").write_bytes(b"tampered")
    assert main(["eval", "--incidents", str(corrupt), "--baseline", *extra]) == 3
    missing = tmp_path / "3" / "label-missing"
    shutil.copytree(FIXTURE, missing / "a-case" / "snapshot")
    assert main(["eval", "--incidents", str(missing), "--baseline", *extra]) == 3
    assert main(["eval", "--incidents", str(tmp_path / "none"), "--baseline", *extra]) == 3


def test_eval_dump_skips_heldout_and_makes_nested_out(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _incidents(tmp_path)
    shutil.copytree(FIXTURE, root / "h-case" / "snapshot")
    (root / "h-case" / "label.yaml").write_bytes(label_yaml("h-case", split="heldout").encode())
    out = tmp_path / "deep" / "er" / "dumps"
    args = ["eval", "--baseline", "dump", "--incidents", str(root), "--dry-run"]
    assert main([*args, "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == ["a-case.dump.txt"]
    assert "skipped heldout incident h-case" in capsys.readouterr().err


def test_eval_summary_line_has_three_decimals(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "r.json"
    args = ["eval", "--baseline", "keyword", "--incidents", str(_incidents(tmp_path))]
    assert main([*args, "--out", str(out)]) == 0
    assert re.search(r"grounding \d\.\d{3} \(n=1\)$", capsys.readouterr().err.strip())
