"""Evidence tests for incident snapshots.

Run against the committed incidents/<id>/snapshot/, or, when SICKBAY_SNAPSHOT_ROOT is
set, against $SICKBAY_SNAPSHOT_ROOT/<id>/ (the weekly re-capture job). With no
snapshots at all every test skips.

Optional one-off checks: set SICKBAY_REAL_USER / SICKBAY_REAL_HOST to the values of the
machine the snapshot was captured on; they must not occur anywhere. Never commit them.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from sickbay.snapshot import Snapshot

REPO = Path(__file__).resolve().parent.parent
MAX_REPO_PATH = 200
# Real hosts use both bare words: systemd ships units like "initrd-cleanup.service" and
# the kernel runs "idle_inject/N" threads (seen on the GitHub Azure kernel). So the
# scripts' file names are banned, not the bare words.
TOOLING_WORDS = ("inject.sh", "cleanup.sh", "incidents/")
_IPV4 = re.compile(rb"(?<![\w.-])(\d{1,3}(?:\.\d{1,3}){3})(?![\w-])(?!\.\w)")
_IPV6 = re.compile(rb"(?<![\w:.-])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![\w:-])(?!\.\w)")


def incident_snapshots() -> dict[str, Path]:
    root = os.environ.get("SICKBAY_SNAPSHOT_ROOT")
    found: dict[str, Path] = {}
    if root:
        for p in sorted(Path(root).glob("*/manifest.json")):
            found[p.parent.name] = p.parent
    else:
        for p in sorted((REPO / "incidents").glob("*/snapshot/manifest.json")):
            found[p.parent.parent.name] = p.parent
    return found


SNAPSHOTS = incident_snapshots()
IDS = sorted(SNAPSHOTS)


def load_label(incident: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO / "incidents" / incident / "label.yaml").read_text("utf-8"))
    assert isinstance(data, dict)
    return data


def all_files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def match_path(pattern: str, path: str) -> bool:
    """fnmatch per path component, so * never crosses a slash."""
    pat, parts = pattern.split("/"), path.split("/")
    return len(pat) == len(parts) and all(
        fnmatch.fnmatchcase(part, pt) for pt, part in zip(pat, parts, strict=True)
    )


def evidence_texts(snap: Snapshot, source: str) -> list[bytes]:
    if source == "journal":
        return [
            "\n".join(str(v) for v in entry.values()).encode("utf-8") for entry in snap.journal()
        ]
    kind, _, ref = source.partition(":")
    if kind == "command":
        return [snap.command(ref).stdout]
    if kind == "file":
        return [
            snap.read_file(f["path"]) for f in snap.manifest["files"] if match_path(ref, f["path"])
        ]
    raise AssertionError(f"unknown evidence source: {source}")


@pytest.fixture(params=IDS)
def incident(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_there_are_snapshots_or_skip() -> None:
    if not IDS:
        pytest.skip("no incident snapshots yet")
    assert {"fd-exhaustion", "memory-ceiling-parent-slice", "restart-loop"} >= set(IDS)


def test_verifies_scrubbed_and_round_trips(incident: str, tmp_path: Path) -> None:
    snap = Snapshot.open(SNAPSHOTS[incident])
    assert snap.verify() == []
    assert snap.manifest["scrubbed"] is True
    snap.write(tmp_path / "copy")
    assert all_files(tmp_path / "copy") == all_files(SNAPSHOTS[incident])


def test_label_is_a_draft_with_required_fields(incident: str) -> None:
    label = load_label(incident)
    assert label["id"] == incident
    assert label["label_schema"] == 0
    assert str(label["status"]).startswith("draft")
    for key in (
        "title",
        "root_cause_category",
        "component",
        "summary",
        "fix",
        "leak_strings",
        "names",
    ):
        assert label[key], key
    assert label["evidence"]


def test_every_evidence_item_is_found(incident: str) -> None:
    snap = Snapshot.open(SNAPSHOTS[incident])
    for item in load_label(incident)["evidence"]:
        needle = item["contains"].encode("utf-8")
        texts = evidence_texts(snap, item["source"])
        assert texts, f"no data for {item['source']}"
        assert any(needle in t for t in texts), f"{item['contains']!r} not in {item['source']}"


def test_no_answer_or_tooling_leaks(incident: str) -> None:
    label = load_label(incident)
    banned = [incident, label["root_cause_category"], *label["leak_strings"], *TOOLING_WORDS]
    files = all_files(SNAPSHOTS[incident])
    for rel, data in files.items():
        text = data.lower()
        for word in banned:
            assert str(word).lower().encode() not in text, f"{word!r} found in {rel}"
        if rel != "manifest.json":
            assert b"sickbay" not in text, f"'sickbay' found in {rel}"


def test_no_real_identity_or_public_ip(incident: str) -> None:
    files = all_files(SNAPSHOTS[incident])
    for rel, data in files.items():
        for m in _IPV4.finditer(data):
            ip = m.group(1).decode()
            ok = ip.startswith(("127.", "192.0.2.")) or ip == "0.0.0.0"
            assert ok, f"non-documentation IPv4 in {rel}"
    doc6 = ipaddress.ip_network("2001:db8::/32")
    for rel, data in files.items():
        for m in _IPV6.finditer(data):
            try:
                ip6 = ipaddress.IPv6Address(m.group(1).decode())
            except ValueError:
                continue  # times, MACs and the like
            ok6 = ip6 in doc6 or ip6 in (ipaddress.IPv6Address("::1"), ipaddress.IPv6Address("::"))
            assert ok6, f"non-documentation IPv6 in {rel}"
    for var in ("SICKBAY_REAL_USER", "SICKBAY_REAL_HOST"):
        value = os.environ.get(var)
        if value:
            needle = value.lower().encode()
            assert not [r for r, d in files.items() if needle in d.lower()], var


def test_no_other_incidents_names_in_snapshot(incident: str) -> None:
    """Catches stale state from an earlier run (for example a journal re-imported at boot)."""
    own = {n.lower() for n in load_label(incident)["names"]}
    foreign = {
        n.lower()
        for other in sorted(p.name for p in (REPO / "incidents").iterdir() if p.is_dir())
        if other != incident
        for n in load_label(other)["names"]
    } - own
    assert foreign
    for rel, data in all_files(SNAPSHOTS[incident]).items():
        low = data.lower()
        for name in foreign:
            assert name.encode() not in low, f"{name!r} (another incident) found in {rel}"


def test_paths_fit_windows_max_path(incident: str) -> None:
    prefix = f"incidents/{incident}/snapshot/"
    longest = max(len(prefix + rel) for rel in all_files(SNAPSHOTS[incident]))
    assert longest < MAX_REPO_PATH
