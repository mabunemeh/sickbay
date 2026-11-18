"""sickbay command line: capture, verify, tool and eval.

Exit codes: 0 ok (for `tool`, any tool result, including an error result), 2 usage
(including a bad or unwritable capture DEST, an unknown tool name, --args that is not a
JSON object, an unwritable eval --out, and `eval --baseline dump` without --dry-run),
3 input unreadable or failing verification (for `eval`, any incident's snapshot or label).
1 (below threshold), 4 and 5 are reserved and unused.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from sickbay import __version__
from sickbay.evals.baselines.dump import build_dump
from sickbay.evals.baselines.keyword import KeywordPredictor
from sickbay.evals.labels import LabelError
from sickbay.evals.runner import (
    dev_incidents,
    dumps_result,
    open_incident,
    run_eval,
    summary_line,
)
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

    ev = sub.add_parser("eval", help="run a baseline over the incidents and score it")
    ev.add_argument("--baseline", required=True, choices=["keyword", "dump"])
    ev.add_argument("--incidents", type=Path, default=Path("incidents"), help="default ./incidents")
    ev.add_argument(
        "--out",
        type=Path,
        help="keyword: result file (default stdout); dump: directory for the dump texts",
    )
    ev.add_argument(
        "--dry-run",
        action="store_true",
        help="dump only: write <incident>.dump.txt files to --out instead of calling a model",
    )
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


def _note(message: str) -> None:
    print(f"sickbay: {message}", file=sys.stderr)


def _eval_keyword(args: argparse.Namespace) -> int:
    if args.dry_run:
        print("sickbay: --dry-run applies to the dump baseline only", file=sys.stderr)
        return EXIT_USAGE
    try:
        result = run_eval(
            KeywordPredictor(),
            args.incidents,
            note=_note,
        )
    except (LabelError, SnapshotError) as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    data = dumps_result(result)
    if args.out is None:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        try:
            args.out.write_bytes(data)
        except OSError as exc:
            print(f"sickbay: cannot write {args.out}: {exc}", file=sys.stderr)
            return EXIT_USAGE
    print(summary_line(result), file=sys.stderr)
    return EXIT_OK


def _eval_dump(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print("sickbay: the dump baseline needs a model provider (batch 4)", file=sys.stderr)
        return EXIT_USAGE
    out: Path | None = args.out
    if out is None:
        print("sickbay: --dry-run needs --out DIR", file=sys.stderr)
        return EXIT_USAGE
    try:
        texts = []
        for incident_dir, label in dev_incidents(args.incidents, _note):
            _, text = build_dump(open_incident(incident_dir))
            texts.append((label.id, text))
    except (LabelError, SnapshotError) as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        out.mkdir(parents=True, exist_ok=True)
        for incident, text in texts:
            (out / f"{incident}.dump.txt").write_bytes(text.encode("utf-8"))
    except OSError as exc:
        print(f"sickbay: cannot write {out}: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"dump: wrote {len(texts)} dump files -> {out}", file=sys.stderr)
    return EXIT_OK


def _cmd_eval(args: argparse.Namespace) -> int:
    if args.baseline == "dump":
        return _eval_dump(args)
    return _eval_keyword(args)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "tool":
        return _cmd_tool(args)
    if args.command == "eval":
        return _cmd_eval(args)
    return _cmd_verify(args)
