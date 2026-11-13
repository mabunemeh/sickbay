"""The ten tools against tests/fixtures/basic, plus validation, caps and the definitions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from recording import RecordingView

from sickbay.snapshot import Snapshot
from sickbay.tools import TOOLS, TOOLSET_VERSION, call_tool, serialize, tool_definitions
from sickbay.tools.base import clip
from sickbay.tools.journal_query import iso_utc
from sickbay.tools.read_file import page
from sickbay.tools.registry import enforce_cap, result_size

FIXTURE = Path(__file__).parent / "fixtures" / "basic"
UNIT = "ledger\\x2dapi.service"


@pytest.fixture(scope="module")
def snap() -> Snapshot:
    return Snapshot.open(FIXTURE)


def call(snap: Snapshot, name: str, **args: Any) -> dict[str, Any]:
    result = call_tool(snap, name, args)
    assert result_size(result) <= 8192
    return result


def error_kind(result: dict[str, Any]) -> str:
    return str(result["error"]["kind"])


# -- definitions -------------------------------------------------------------------------------


def test_ten_tools_with_strict_schemas() -> None:
    assert TOOLSET_VERSION == 1
    names = [d["name"] for d in tool_definitions()]
    assert names == [
        "list_units",
        "unit_status",
        "journal_query",
        "process_list",
        "process_limits",
        "cgroup_tree",
        "disk_usage",
        "sockets",
        "read_file",
        "list_dir",
    ]
    for d in tool_definitions():
        schema = d["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert d["description"]
        for key in schema.get("required", []):
            assert key in schema["properties"]
        for prop in schema["properties"].values():
            assert prop["type"] in ("string", "integer")
            if prop["type"] == "integer":
                assert prop["minimum"] <= prop["maximum"]
        json.dumps(d)


def test_unknown_tool_and_bad_args(snap: Snapshot) -> None:
    assert error_kind(call_tool(snap, "dns_check", {})) == "unknown_tool"
    assert error_kind(call_tool(snap, 7, {})) == "unknown_tool"
    long_name = "x" * 5000
    result = call_tool(snap, long_name, {})
    assert error_kind(result) == "unknown_tool" and len(result["error"]["message"]) < 200
    assert error_kind(call_tool(snap, "list_units", [])) == "invalid_argument"
    assert error_kind(call_tool(snap, "list_units", None)) == "invalid_argument"
    assert error_kind(call_tool(snap, "list_units", {"bogus": 1})) == "invalid_argument"
    assert error_kind(call_tool(snap, "unit_status", {})) == "invalid_argument"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_units", {"limit": 0}),
        ("list_units", {"limit": 201}),
        ("list_units", {"limit": True}),
        ("list_units", {"limit": 5.0}),
        ("list_units", {"limit": "5"}),
        ("list_units", {"state": "running"}),
        ("list_units", {"type": "Service"}),
        ("list_units", {"name_contains": "x" * 257}),
        ("list_units", {"name_contains": "a\nb"}),
        ("journal_query", {"priority_max": 8}),
        ("journal_query", {"priority_max": -1}),
        ("journal_query", {"order": "random"}),
        ("journal_query", {"contains": "\ud800"}),
        ("cgroup_tree", {"depth": 4}),
        ("cgroup_tree", {"depth": 0}),
        ("read_file", {"path": "/proc/meminfo", "max_bytes": 6001}),
        ("read_file", {"path": "/proc/meminfo", "offset": -1}),
        ("process_limits", {"pid": 200}),
        ("process_limits", {"pid": "0200"}),
        ("process_limits", {"pid": "２００"}),
        ("sockets", {"port": 65536}),
    ],
)
def test_schema_validation_rejects(snap: Snapshot, name: str, args: dict[str, Any]) -> None:
    assert error_kind(call_tool(snap, name, args)) == "invalid_argument"


def test_string_argument_at_256_bytes_is_accepted(snap: Snapshot) -> None:
    result = call(snap, "list_units", name_contains="é" * 128)
    assert result["total"] == 0


# -- tools on the fixture ---------------------------------------------------------------------


def test_list_units(snap: Snapshot) -> None:
    result = call(snap, "list_units")
    assert result["total"] == 5 and result["truncated"] is False
    assert result["units"][1] == {
        "unit": UNIT,
        "load": "loaded",
        "active": "active",
        "sub": "running",
        "description": "Ledger API",
    }
    assert [u["unit"] for u in call(snap, "list_units", type="slice")["units"]] == [
        "-.slice",
        "batch.slice",
    ]
    assert [u["unit"] for u in call(snap, "list_units", state="inactive")["units"]] == [
        "ghost.service"
    ]
    assert call(snap, "list_units", name_contains="LEDGER")["total"] == 0
    capped = call(snap, "list_units", limit=2)
    assert (capped["total"], capped["returned"], capped["truncated"]) == (5, 2, True)
    assert len(capped["units"]) == 2


def test_unit_status(snap: Snapshot) -> None:
    result = call(snap, "unit_status", unit=UNIT)
    assert result["unit"] == UNIT
    assert result["properties"]["MainPID"] == "200"
    assert result["properties"]["Environment"] == "API_KEY=<redacted> MODE=<redacted>"
    assert call(snap, "unit_status", unit="batch.slice")["properties"] == {"Id": "batch.slice"}
    assert error_kind(call(snap, "unit_status", unit="cron.timer")) == "not_found"
    assert error_kind(call(snap, "unit_status", unit="..")) == "invalid_argument"


def test_journal_query(snap: Snapshot) -> None:
    result = call(snap, "journal_query")
    assert result["total"] == 2
    newest, oldest = result["entries"]
    assert newest == {
        "time": {"usec": "1700000002000000", "utc": "2023-11-14T22:13:22.000000Z"},
        "unit": UNIT,
        "identifier": "ledger-api",
        "pid": "200",
        "priority": "3",
        "message": "ledger-api: connect from 192.0.2.2 failed",
    }
    assert oldest["unit"] == UNIT and oldest["message"] == "started é ok"
    first = call(snap, "journal_query", order="oldest")["entries"][0]
    assert first["message"] == "started é ok"
    assert call(snap, "journal_query", unit=UNIT)["total"] == 2
    assert call(snap, "journal_query", unit="init.scope")["total"] == 1
    assert call(snap, "journal_query", priority_max=3)["total"] == 1
    assert call(snap, "journal_query", contains="connect from")["total"] == 1
    assert call(snap, "journal_query", contains="CONNECT")["total"] == 0
    assert call(snap, "journal_query", contains="conn.ct")["total"] == 0  # no regex
    capped = call(snap, "journal_query", limit=1)
    assert capped["returned"] == 1 and capped["truncated"] is True


def test_journal_binary_message_and_bad_timestamp() -> None:

    view = RecordingView(
        journal=[
            {"MESSAGE": [104, 105, 0xFF], "__REALTIME_TIMESTAMP": "x"},
            {"MESSAGE": ["a", "b"], "PRIORITY": "high"},
        ]
    )
    result = call_tool(view, "journal_query", {"order": "oldest"})
    first, second = result["entries"]
    assert first["message"] == "hi�" and first["time"] == {"usec": "x"}
    assert second["message"] == '["a", "b"]'
    assert call_tool(view, "journal_query", {"priority_max": 7})["total"] == 0
    assert iso_utc("99999999999999999999999") is None


def test_process_list(snap: Snapshot) -> None:
    result = call(snap, "process_list")
    assert [p["pid"] for p in result["processes"]] == ["1", "200"]
    assert result["processes"][1] == {
        "pid": "200",
        "ppid": "1",
        "user": "user1000",
        "stat": "Ssl",
        "rss": "7000",
        "vsz": "90000",
        "nlwp": "4",
        "etimes": "40",
        "args": "/usr/bin/python3 /srv/ledger/app.py",
    }
    assert [p["pid"] for p in call(snap, "process_list", sort="rss")["processes"]] == ["200", "1"]
    assert call(snap, "process_list", name_contains="ledger")["total"] == 1


def test_process_limits(snap: Snapshot) -> None:
    result = call(snap, "process_limits", pid="200")
    assert result["pid"] == "200" and result["open_fds"] == 3
    assert result["limits"] == [
        {"limit": "Max processes", "soft": "63366", "hard": "63366", "units": "processes"},
        {"limit": "Max open files", "soft": "512", "hard": "512", "units": "files"},
    ]
    assert result["total"] == 2 and result["truncated"] is False
    assert error_kind(call(snap, "process_limits", pid="4242")) == "not_found"
    only_limits = call(snap, "process_limits", pid="1")
    assert "open_fds" not in only_limits and only_limits["limits"] == []


def test_cgroup_tree(snap: Snapshot) -> None:
    result = call(snap, "cgroup_tree")
    assert [n["path"] for n in result["nodes"]] == ["/", "/batch.slice", "/system.slice"]
    assert result["nodes"][0] == {"path": "/", "procs_count": 2}
    assert result["nodes"][1] == {
        "path": "/batch.slice",
        "memory.max": "67108864\n",
        "memory.events": "low 0\nhigh 0\nmax 4\noom 1\noom_kill 1\n",
    }
    deep = call(snap, "cgroup_tree", path="/system.slice", depth=3)
    assert deep["nodes"][1] == {
        "path": f"/system.slice/{UNIT}",
        "memory.current": "52428800\n",
        "memory.max": "max\n",
        "pids.current": "3\n",
        "procs_count": 1,
    }
    assert error_kind(call(snap, "cgroup_tree", path="/nope.slice")) == "not_found"
    assert error_kind(call(snap, "cgroup_tree", path="/cgroup.procs")) == "not_a_directory"
    assert error_kind(call(snap, "cgroup_tree", path="system.slice")) == "invalid_argument"
    assert error_kind(call(snap, "cgroup_tree", path="/../../etc")) == "invalid_argument"


def test_x2d_unit_file_and_cgroup_are_readable(snap: Snapshot) -> None:
    unit_file = call(snap, "read_file", path=f"/etc/systemd/system/{UNIT}")
    assert "LimitNOFILE=512" in unit_file["content"]
    cg = call(snap, "cgroup_tree", path=f"/system.slice/{UNIT}")
    assert cg["nodes"] == [deep_node()]
    memory = call(snap, "read_file", path=f"/sys/fs/cgroup/system.slice/{UNIT}/memory.current")
    assert memory["content"] == "52428800\n"
    listing = call(snap, "list_dir", path=f"/sys/fs/cgroup/system.slice/{UNIT}")
    assert [e["name"] for e in listing["entries"]] == [
        "cgroup.procs",
        "memory.current",
        "memory.max",
        "pids.current",
    ]


def deep_node() -> dict[str, Any]:
    return {
        "path": f"/system.slice/{UNIT}",
        "memory.current": "52428800\n",
        "memory.max": "max\n",
        "pids.current": "3\n",
        "procs_count": 1,
    }


def test_cgroup_tree_node_cap() -> None:

    files = {f"/sys/fs/cgroup/g{i:03}.slice/pids.max": b"max\n" for i in range(150)}
    result = call_tool(RecordingView(files=files), "cgroup_tree", {})
    assert result["total"] == 151 and result["truncated"] is True
    assert result["returned"] == len(result["nodes"]) <= 100


def test_disk_usage(snap: Snapshot) -> None:
    result = call(snap, "disk_usage")
    assert result["total"] == 2
    assert result["mounts"][1] == {
        "filesystem": "tmpfs",
        "mounted_on": "/run/spool area",
        "1024-blocks": "2048",
        "used": "0",
        "available": "2048",
        "capacity": "0%",
        "inodes": "4096",
        "iused": "1",
        "ifree": "4095",
        "iuse%": "1%",
    }


def test_sockets(snap: Snapshot) -> None:
    result = call(snap, "sockets")
    assert result["sockets"][0] == {
        "netid": "tcp",
        "state": "LISTEN",
        "local": "192.0.2.1:8080",
        "peer": "0.0.0.0:*",
        "process": 'users:(("python3",pid=200,fd=3))',
    }
    assert result["sockets"][1]["process"] == ""
    assert [s["local"] for s in call(snap, "sockets", port=53)["sockets"]] == ["127.0.0.53%lo:53"]
    assert call(snap, "sockets", port=80)["total"] == 0


def test_read_file(snap: Snapshot) -> None:
    result = call(snap, "read_file", path="/proc/200/cmdline")
    assert result == {
        "path": "/proc/200/cmdline",
        "size": 53,
        "offset": 0,
        "returned": 53,
        "content": "/usr/bin/python3 /srv/ledger/app.py --owner=user1000 ",
        "truncated": False,
    }
    paged = call(snap, "read_file", path="/proc/200/cmdline", offset=17, max_bytes=18)
    assert paged["content"] == "/srv/ledger/app.py" and paged["truncated"] is True
    end = call(snap, "read_file", path="/proc/200/cmdline", offset=53)
    assert end["content"] == "" and end["returned"] == 0
    assert error_kind(call(snap, "read_file", path="/proc/200/cmdline", offset=54)) == (
        "invalid_argument"
    )
    assert error_kind(call(snap, "read_file", path="/proc/9/limits")) == "not_found"
    assert error_kind(call(snap, "read_file", path="/etc/systemd")) == "is_directory"
    assert error_kind(call(snap, "read_file", path="/proc/200/environ")) == "not_allowed"


def test_read_file_symlink_is_not_followed(snap: Snapshot) -> None:
    result = call(snap, "read_file", path="/etc/os-release")
    assert error_kind(result) == "is_symlink"
    assert result["target"] == "../usr/lib/os-release" and "content" not in result
    # The target is resolved by the model, not by the tool, and is not allowlisted.
    assert error_kind(call(snap, "read_file", path="/usr/lib/os-release")) == "not_allowed"


def test_list_dir(snap: Snapshot) -> None:
    root = call(snap, "list_dir", path="/")
    assert root["entries"] == [
        {"name": "etc", "type": "dir"},
        {"name": "proc", "type": "dir"},
        {"name": "run", "type": "dir"},
        {"name": "sys", "type": "dir"},
    ]
    etc = call(snap, "list_dir", path="/etc")
    assert etc["entries"] == [
        {"name": "os-release", "type": "symlink", "target": "../usr/lib/os-release"},
        {"name": "security", "type": "dir"},
        {"name": "systemd", "type": "dir"},
    ]
    wants = call(snap, "list_dir", path="/etc/systemd/system/multi-user.target.wants")
    assert wants["entries"] == [
        {"name": UNIT, "type": "symlink", "target": f"/etc/systemd/system/{UNIT}"}
    ]
    proc = call(snap, "list_dir", path="/proc")
    assert [e["name"] for e in proc["entries"]] == ["1", "200", "loadavg", "meminfo", "sys"]
    assert call(snap, "list_dir", path="/proc", limit=2)["truncated"] is True
    assert error_kind(call(snap, "list_dir", path="/proc/meminfo")) == "not_a_directory"
    assert error_kind(call(snap, "list_dir", path="/etc/os-release")) == "is_symlink"
    assert error_kind(call(snap, "list_dir", path="/proc/pressure")) == "not_found"
    assert error_kind(call(snap, "list_dir", path="/usr")) == "not_allowed"


def test_list_dir_hides_children_that_lead_nowhere() -> None:

    view = RecordingView(
        files={
            "/proc/7/environ": b"SECRET=x",
            "/proc/7/limits": b"",
            "/proc/self/limits": b"",
            "/etc/shadow": b"x",
            "/etc/security/access.conf": b"x",
            "/etc/security/limits.conf": b"x",
        }
    )
    assert [e["name"] for e in call_tool(view, "list_dir", {"path": "/proc/7"})["entries"]] == [
        "limits"
    ]
    assert [e["name"] for e in call_tool(view, "list_dir", {"path": "/proc"})["entries"]] == ["7"]
    etc = call_tool(view, "list_dir", {"path": "/etc/security"})
    assert [e["name"] for e in etc["entries"]] == ["limits.conf"] and etc["total"] == 1
    assert [e["name"] for e in call_tool(view, "list_dir", {"path": "/etc"})["entries"]] == [
        "security"
    ]


# -- caps --------------------------------------------------------------------------------------


def test_clip_at_limit_one_over_and_multibyte_boundary() -> None:
    assert clip("a" * 2048) == "a" * 2048
    assert clip("a" * 2049) == "a" * 2048 + "…[+1 bytes]"
    # "é" is 2 bytes: 2047 + 2 = 2049 bytes, the cut would split it, so it moves back.
    assert clip("a" * 2047 + "é") == "a" * 2047 + "…[+2 bytes]"
    assert clip("€" * 3, 4) == "€…[+6 bytes]"
    assert clip("\udcff") == "?"


def test_read_file_page_never_splits_a_character() -> None:
    data = "é€😀".encode()  # 2 + 3 + 4 bytes
    assert page(data, 0, 4) == "é".encode()
    assert page(data, 0, 5) == "é€".encode()
    assert page(data, 0, 6) == "é€".encode()
    assert page(data, 0, 7) == "é€".encode()
    assert page(data, 0, 8) == "é€".encode()
    assert page(data, 0, 9) == data
    assert page(data, 2, 4) == "€".encode()
    assert page(data, 5, 4) == "😀".encode()


def _pages(view: RecordingView, max_bytes: int) -> tuple[bytes, list[str]]:
    raw, texts, offset = b"", [], 0
    data = view.files["/proc/meminfo"]
    while True:
        result = call_tool(
            view, "read_file", {"path": "/proc/meminfo", "offset": offset, "max_bytes": max_bytes}
        )
        raw += data[offset : offset + result["returned"]]
        texts.append(result["content"])
        offset += result["returned"]
        assert result["returned"] > 0 or not result["truncated"]
        if not result["truncated"]:
            return raw, texts


@pytest.mark.parametrize("max_bytes", [4, 5, 6, 7, 11])
def test_read_file_pages_round_trip(max_bytes: int) -> None:
    valid = ("é€😀" * 5 + "abc").encode()
    raw, texts = _pages(RecordingView(files={"/proc/meminfo": valid}), max_bytes)
    assert raw == valid
    assert "".join(texts) == valid.decode("utf-8")  # no character is ever split
    invalid = valid + b"\xff\xfe" + "€".encode()[:2] + "x😀".encode()
    raw, texts = _pages(RecordingView(files={"/proc/meminfo": invalid}), max_bytes)
    assert raw == invalid  # invalid bytes are consumed, so offset += returned is exact
    assert "".join(texts).endswith("x😀")


def test_read_file_max_bytes_minimum_is_4(snap: Snapshot) -> None:
    for bad in (1, 3):
        result = call_tool(snap, "read_file", {"path": "/proc/loadavg", "max_bytes": bad})
        assert error_kind(result) == "invalid_argument"
    assert call(snap, "read_file", path="/proc/loadavg", max_bytes=4)["returned"] == 4


def _padded(rows: int, target: int) -> dict[str, Any]:
    """A rows result whose serialised size is exactly target bytes."""
    result: dict[str, Any] = {
        "rows": [{"v": "x"} for _ in range(rows)],
        "total": rows,
        "returned": rows,
        "truncated": False,
        "pad": "",
    }
    result["pad"] = "p" * (target - result_size(result))
    assert result_size(result) == target
    return result


def test_global_cap_at_limit_and_one_byte_over() -> None:
    at = _padded(100, 8192)
    assert enforce_cap(at, "rows") is at
    over = _padded(100, 8193)
    capped = enforce_cap(over, "rows")
    assert result_size(capped) <= 8192
    assert capped["truncated"] is True and capped["total"] == 100
    assert capped["returned"] == len(capped["rows"]) == 99  # one row is enough to fit
    assert enforce_cap(_padded(0, 8193), "rows")["error"]["kind"] == "output_too_large"
    assert enforce_cap(_padded(1, 8193), None)["error"]["kind"] == "output_too_large"


def test_global_cap_with_multibyte_rows() -> None:
    result: dict[str, Any] = {
        "rows": [{"v": "€" * 600} for _ in range(10)],
        "total": 10,
        "returned": 10,
        "truncated": False,
    }
    capped = enforce_cap(result, "rows")
    assert result_size(capped) <= 8192 < result_size({**capped, "rows": result["rows"][:5]})
    assert capped["returned"] == 4


def test_global_cap_through_call_tool() -> None:

    ps = "".join(f"{pid} 1 root S 1 1 1 1 {'x' * 600}\n" for pid in range(1, 201))
    result = call_tool(RecordingView(commands={"ps": ps.encode()}), "process_list", {"limit": 200})
    assert result_size(result) <= 8192
    assert result["total"] == 200 and result["truncated"] is True
    assert 0 < result["returned"] == len(result["processes"]) < 200
    assert result["processes"][0]["args"] == "x" * 512 + "…[+88 bytes]"


def test_journal_message_cap() -> None:

    view = RecordingView(journal=[{"MESSAGE": "m" * 1030}])
    message = call_tool(view, "journal_query", {})["entries"][0]["message"]
    assert message == "m" * 1024 + "…[+6 bytes]"


ONE_CALL_EACH: dict[str, dict[str, Any]] = {
    "list_units": {},
    "unit_status": {"unit": UNIT},
    "journal_query": {},
    "process_list": {},
    "process_limits": {"pid": "200"},
    "cgroup_tree": {"depth": 3},
    "disk_usage": {},
    "sockets": {},
    "read_file": {"path": "/proc/200/limits"},
    "list_dir": {"path": "/proc/200"},
}


def test_serialisation_is_deterministic(snap: Snapshot) -> None:
    assert set(ONE_CALL_EACH) == set(TOOLS)
    for name, args in ONE_CALL_EACH.items():
        first = serialize(call_tool(snap, name, args))
        assert first == serialize(call_tool(snap, name, dict(args)))
        assert '"error"' not in first, (name, first)
        canonical = json.dumps(
            json.loads(first), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        assert first == canonical


# -- review round: boundaries that mutants slipped through -------------------------------------


def test_path_argument_length_boundary() -> None:
    from sickbay.tools.paths import check_path_syntax

    exact = "/etc/systemd/" + "a" * (256 - len("/etc/systemd/"))
    assert len(exact.encode()) == 256 and check_path_syntax(exact) == exact
    view = RecordingView()
    assert error_kind(call_tool(view, "read_file", {"path": exact})) == "not_found"
    over = exact + "a"
    assert error_kind(call_tool(view, "read_file", {"path": over})) == "invalid_argument"
    two_byte = "/etc/systemd/" + "é" * 121 + "a"  # 13 + 242 + 1 = 256 bytes
    assert len(two_byte.encode()) == 256 and check_path_syntax(two_byte) == two_byte


def test_default_row_limit_is_50_and_default_max_bytes_is_4096() -> None:
    ps = "".join(f"{pid} 1 root S 1 1 1 1 p\n" for pid in range(1, 61))
    view = RecordingView(commands={"ps": ps.encode()}, files={"/proc/meminfo": b"m" * 5000})
    result = call_tool(view, "process_list", {})
    assert (result["total"], result["returned"], result["truncated"]) == (60, 50, True)
    page_ = call_tool(view, "read_file", {"path": "/proc/meminfo"})
    assert page_["returned"] == 4096 and len(page_["content"]) == 4096


def test_global_cap_keeps_the_largest_prefix_that_fits_exactly() -> None:
    rows = [{"v": "x" * 90} for _ in range(100)]
    result: dict[str, Any] = {"rows": rows, "total": 100, "returned": 100, "truncated": False}
    keep = 60
    trial = {**result, "rows": rows[:keep], "returned": keep, "truncated": True, "pad": ""}
    result["pad"] = "p" * (8192 - result_size(trial))
    capped = enforce_cap(result, "rows")
    assert result_size(capped) == 8192
    assert capped["returned"] == keep and len(capped["rows"]) == keep


def test_journal_equal_timestamps_keep_file_order() -> None:
    """Ties: oldest keeps file order; newest is its exact reverse."""
    view = RecordingView(
        journal=[
            {"MESSAGE": "first", "__REALTIME_TIMESTAMP": "5"},
            {"MESSAGE": "second", "__REALTIME_TIMESTAMP": "5"},
            {"MESSAGE": "early", "__REALTIME_TIMESTAMP": "1"},
        ]
    )
    oldest = [
        e["message"] for e in call_tool(view, "journal_query", {"order": "oldest"})["entries"]
    ]
    newest = [e["message"] for e in call_tool(view, "journal_query", {})["entries"]]
    assert oldest == ["early", "first", "second"]
    assert newest == ["second", "first", "early"]


def test_list_units_bullet_and_exact_type_suffix() -> None:
    listing = (
        "● bad.service loaded failed failed Broken thing\n"
        "* star.service loaded failed failed Star\n"
        "service-helper.socket loaded active listening Helper socket\n"
        "x.service.d.mount loaded active mounted Odd mount\n"
    )
    view = RecordingView(commands={"systemctl-list-units": listing.encode()})
    rows = call_tool(view, "list_units", {})["units"]
    assert rows[0] == {
        "unit": "bad.service",
        "load": "loaded",
        "active": "failed",
        "sub": "failed",
        "description": "Broken thing",
    }
    services = call_tool(view, "list_units", {"type": "service"})["units"]
    assert [u["unit"] for u in services] == ["bad.service", "star.service"]
    sockets = call_tool(view, "list_units", {"type": "socket"})["units"]
    assert [u["unit"] for u in sockets] == ["service-helper.socket"]


def test_unit_status_repeated_key_becomes_a_list() -> None:
    show = "Id=a.service\nExecStart=one\nUser=\nExecStart=two\nExecStart=three\n\nId=b.service\n"
    view = RecordingView(commands={"systemctl-show": show.encode()})
    props = call_tool(view, "unit_status", {"unit": "a.service"})["properties"]
    assert props == {"Id": "a.service", "ExecStart": ["one", "two", "three"], "User": ""}


def test_unavailable_only_for_a_missing_command() -> None:
    assert error_kind(call_tool(RecordingView(), "list_units", {})) == "unavailable"
