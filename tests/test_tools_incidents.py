"""Every tool on every incident snapshot, and every label evidence string reached through tools."""

from __future__ import annotations

from typing import Any

import pytest
from test_incidents import IDS, SNAPSHOTS, load_label, match_path

from sickbay.snapshot import Snapshot
from sickbay.tools import TOOLS, call_tool
from sickbay.tools.registry import result_size


@pytest.fixture(params=IDS)
def incident(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def ok(snap: Snapshot, name: str, **args: Any) -> dict[str, Any]:
    result = call_tool(snap, name, args)
    assert "error" not in result, (name, args, result)
    assert result_size(result) <= 8192
    return result


def read_whole(snap: Snapshot, path: str) -> str:
    """read_file paged to the end, as a model would."""
    out, offset = [], 0
    while True:
        page = ok(snap, "read_file", path=path, offset=offset, max_bytes=6000)
        out.append(page["content"])
        offset += page["returned"]
        if not page["truncated"]:
            return "".join(out)


def expand(snap: Snapshot, pattern: str) -> list[str]:
    """Expand a label source glob by walking list_dir, never by reading the manifest."""
    paths = ["/"]
    parts = pattern.strip("/").split("/")
    for depth, part in enumerate(parts):
        nxt = []
        for base in paths:
            listing = ok(snap, "list_dir", path=base, limit=200)
            assert listing["truncated"] is False
            for entry in listing["entries"]:
                is_last = depth == len(parts) - 1
                wanted = entry["type"] == ("file" if is_last else "dir")
                if wanted and match_path(f"/{part}", f"/{entry['name']}"):
                    nxt.append(f"{base.rstrip('/')}/{entry['name']}")
        paths = nxt
    assert paths, f"nothing matches {pattern}"
    return paths


def unit_texts(snap: Snapshot, names: list[str]) -> list[str]:
    texts = []
    for name in names:
        for row in ok(snap, "list_units", name_contains=name, limit=200)["units"]:
            result = call_tool(snap, "unit_status", {"unit": row["unit"]})
            if "properties" in result:
                texts.append("\n".join(f"{k}={v}" for k, v in result["properties"].items()))
    return texts


def test_every_tool_runs_on_the_incident(incident: str) -> None:
    snap = Snapshot.open(SNAPSHOTS[incident])
    label = load_label(incident)
    seen = set()

    def run(name: str, **args: Any) -> dict[str, Any]:
        seen.add(name)
        return ok(snap, name, **args)

    assert run("list_units", name_contains=label["names"][0])["total"] >= 1
    assert run("unit_status", unit=label["component"])["unit"] == label["component"]
    assert run("journal_query", priority_max=6)["returned"] > 0
    assert run("process_list", sort="rss")["processes"][0]["pid"].isdigit()
    assert run("process_limits", pid="1")["open_fds"] > 0
    assert run("cgroup_tree", path="/system.slice")["nodes"][0]["path"] == "/system.slice"
    assert any(m["mounted_on"] == "/" for m in run("disk_usage")["mounts"])
    assert run("sockets")["total"] > 0
    assert "MemTotal:" in run("read_file", path="/proc/meminfo")["content"]
    assert {"etc", "proc", "sys"} <= {e["name"] for e in run("list_dir", path="/")["entries"]}
    assert seen == set(TOOLS)


def test_evidence_is_reachable_through_tools(incident: str) -> None:
    snap = Snapshot.open(SNAPSHOTS[incident])
    label = load_label(incident)
    for item in label["evidence"]:
        needle, source = item["contains"], item["source"]
        kind, _, ref = source.partition(":")
        if kind == "journal":
            entries = ok(snap, "journal_query", contains=needle, limit=200)["entries"]
            texts = [e["message"] for e in entries]
        elif source == "command:systemctl-show":
            texts = unit_texts(snap, label["names"])
        elif kind == "file":
            texts = [read_whole(snap, p) for p in expand(snap, ref)]
        else:
            raise AssertionError(f"no tool route for {source}")
        assert any(needle in t for t in texts), f"{needle!r} not reachable via tools ({source})"


def test_incident_specific_tool_views(incident: str) -> None:
    snap = Snapshot.open(SNAPSHOTS[incident])
    if incident == "fd-exhaustion":
        procs = ok(snap, "process_list", name_contains="ledger")["processes"]
        limits = ok(snap, "process_limits", pid=procs[0]["pid"])
        nofile = next(r for r in limits["limits"] if r["limit"] == "Max open files")
        assert (nofile["soft"], nofile["hard"]) == ("512", "512")
        assert limits["open_fds"] >= 512
    elif incident == "memory-ceiling-parent-slice":
        node = ok(snap, "cgroup_tree", path="/batch.slice")["nodes"][0]
        assert node["memory.max"] == "67108864\n"
    else:
        props = ok(snap, "unit_status", unit="quote-sync.service")["properties"]
        assert props["Restart"] == "always" and props["Result"] == "exit-code"


def _real(incident: str) -> Snapshot:
    if incident not in SNAPSHOTS:
        pytest.skip(f"{incident} snapshot not present")
    return Snapshot.open(SNAPSHOTS[incident])


def test_repeated_execstart_is_kept_in_order() -> None:
    snap = _real("fd-exhaustion")
    props = ok(snap, "unit_status", unit="snapd.system-shutdown.service")["properties"]
    starts = props["ExecStart"]
    assert isinstance(starts, list) and len(starts) == 3
    assert "argv[]=/bin/mount /run -o remount,exec" in starts[0]
    assert "argv[]=/bin/mkdir -p /run/initramfs" in starts[1]
    assert "argv[]=/bin/cp /usr/lib/snapd/system-shutdown /run/initramfs/shutdown" in starts[2]
    assert isinstance(props["Id"], str)


def test_process_limits_columns_against_real_kernel_text() -> None:
    snap = _real("fd-exhaustion")
    pid = ok(snap, "process_list", name_contains="ledger")["processes"][0]["pid"]
    rows = {r["limit"]: r for r in ok(snap, "process_limits", pid=pid)["limits"]}
    assert len(rows) == 16
    assert rows["Max cpu time"] == {
        "limit": "Max cpu time",
        "soft": "unlimited",
        "hard": "unlimited",
        "units": "seconds",
    }
    assert rows["Max stack size"] == {
        "limit": "Max stack size",
        "soft": "8388608",
        "hard": "unlimited",
        "units": "bytes",
    }
    assert rows["Max open files"] == {
        "limit": "Max open files",
        "soft": "512",
        "hard": "512",
        "units": "files",
    }
    assert rows["Max processes"]["units"] == "processes"
    assert rows["Max nice priority"] == {
        "limit": "Max nice priority",
        "soft": "0",
        "hard": "0",
        "units": "",
    }
    assert rows["Max realtime timeout"] == {
        "limit": "Max realtime timeout",
        "soft": "unlimited",
        "hard": "unlimited",
        "units": "us",
    }
