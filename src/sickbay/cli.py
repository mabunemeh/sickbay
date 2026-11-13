"""sickbay command line: capture, verify and tool.

Exit codes: 0 ok (for `tool`, any tool result, including an error result), 2 usage
(including a bad or unwritable capture DEST, an unknown tool name, or --args that is not a
JSON object), 3 input unreadable or failing verification.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sickbay import __version__
from sickbay.snapshot import Snapshot, SnapshotError, capture
from sickbay.snapshot.source import LiveSource
from sickbay.tools import TOOLS, call_tool, serialize

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_UNREADABLE = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sickbay", description=__doc__.split("\n")[0])
    parser.add_argument("--version", action="version", version=f"sickbay {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="capture a snapshot of this host")
    cap.add_argument(
        "dest",
        type=Path,
        help="directory to create (must not exist or be empty; exit 2 if bad or unwritable)",
    )
    cap.add_argument(
        "--since",
        default="-1h",
        help="journal start, a journalctl --since value; write --since=-1h for relative times",
    )
    cap.add_argument(
        "--no-scrub",
        action="store_true",
        help="skip scrubbing (local debugging only; the snapshot is marked scrubbed: false)",
    )

    ver = sub.add_parser("verify", help="check a snapshot's hashes and file list")
    ver.add_argument("snapshot", type=Path)

    tool = sub.add_parser("tool", help="run one read-only tool against a snapshot")
    tool.add_argument("snapshot", type=Path)
    tool.add_argument("name", help=f"tool name: {', '.join(TOOLS)}")
    tool.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    return parser


def _cmd_capture(args: argparse.Namespace) -> int:
    dest: Path = args.dest
    if not sys.platform.startswith("linux"):
        print("sickbay: capture runs on Linux hosts only", file=sys.stderr)
        return EXIT_USAGE
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        print(f"sickbay: destination exists and is not empty: {dest}", file=sys.stderr)
        return EXIT_USAGE
    try:
        manifest = capture(LiveSource(args.since), dest, since=args.since, scrub=not args.no_scrub)
    except OSError as exc:
        print(f"sickbay: cannot write snapshot: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(
        f"captured {len(manifest['files'])} files, {len(manifest['commands'])} commands, "
        f"{manifest['journal']['entries']} journal entries, "
        f"{len(manifest['errors'])} errors -> {dest}"
    )
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    try:
        problems = Snapshot.open(args.snapshot).verify()
    except SnapshotError as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    if problems:
        for problem in problems:
            print(f"sickbay: {problem}", file=sys.stderr)
        return EXIT_UNREADABLE
    print(f"ok: {args.snapshot}")
    return EXIT_OK


def _cmd_tool(args: argparse.Namespace) -> int:
    if args.name not in TOOLS:
        print(f"sickbay: unknown tool {args.name!r}; tools: {', '.join(TOOLS)}", file=sys.stderr)
        return EXIT_USAGE
    try:
        tool_args = json.loads(args.args)
    except ValueError as exc:
        print(f"sickbay: --args is not valid JSON: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not isinstance(tool_args, dict):
        print("sickbay: --args must be a JSON object", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = call_tool(Snapshot.open(args.snapshot), args.name, tool_args)
    except SnapshotError as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    out = (serialize(result) + "\n").encode("utf-8")
    sys.stdout.flush()
    sys.stdout.buffer.write(out)
    sys.stdout.buffer.flush()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "tool":
        return _cmd_tool(args)
    return _cmd_verify(args)
