"""sickbay command line: capture, verify, tool and eval.

Exit codes:
  0 ok (for `tool`, any tool result, including an error result);
  2 usage: a bad or unwritable capture DEST, an unknown tool name, --args that is not a
    JSON object, an unwritable eval --out, and for model runs a missing --model, --max-usd
    or --out, a --max-usd that is not a positive number, a model with no price, an unset
    ANTHROPIC_API_KEY, or the anthropic SDK not installed;
  3 input unreadable or failing verification (for `eval`, any incident's snapshot or
    label, or the --replay recording), and a replay mismatch (no result is written);
  4 provider error: the run stopped; the partial result and recording are written;
  5 budget cap hit: unstarted cases are budget_exceeded; result and recording are written.
1 (below threshold) is reserved and unused.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sickbay import __version__
from sickbay.agent.loop import AgentPredictor
from sickbay.agent.pricing import Budget, UnknownPrice, price_for
from sickbay.agent.providers import (
    Provider,
    ProviderSetupError,
    RecordingError,
    RecordingProvider,
    ReplayMismatch,
    ReplayProvider,
    dumps_recording,
    load_recording,
    recording_path,
)
from sickbay.agent.request import DEFAULT_EFFORT, EFFORTS
from sickbay.evals.baselines.dump import DumpPredictor, build_dump
from sickbay.evals.baselines.keyword import KeywordPredictor
from sickbay.evals.labels import LabelError
from sickbay.evals.predictor import Predictor
from sickbay.evals.runner import (
    RunAborted,
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
EXIT_PROVIDER = 4
EXIT_BUDGET = 5

PROVIDERS = ("anthropic",)
MODEL_PREDICTORS: dict[str, type[AgentPredictor] | type[DumpPredictor]] = {
    "agent": AgentPredictor,
    "dump": DumpPredictor,
}
_MODEL_FLAGS = ("model", "effort", "max_usd", "repeats", "provider")


def _positive_usd(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number: {text!r}")
    return value


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1: {text!r}")
    return value


def _make_provider(name: str) -> Provider:
    """The live provider. Raises ProviderSetupError (exit 2) without the SDK or a key."""
    from sickbay.agent.providers.anthropic import AnthropicProvider

    return AnthropicProvider.from_env()


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

    ev = sub.add_parser("eval", help="run a baseline or the agent over the incidents, and score")
    mode = ev.add_mutually_exclusive_group(required=True)
    mode.add_argument("--baseline", choices=["keyword", "dump"])
    mode.add_argument("--agent", action="store_true", help="run the tool-calling agent")
    mode.add_argument(
        "--replay",
        type=Path,
        metavar="RECORDING",
        help="re-score a recorded model run from PATH.recording.json, with no network",
    )
    ev.add_argument("--incidents", type=Path, default=Path("incidents"), help="default ./incidents")
    ev.add_argument(
        "--out",
        type=Path,
        help="result file (keyword: default stdout; model runs and --replay: required, and a "
        "model run also writes PATH.recording.json beside it); dump --dry-run: a directory",
    )
    ev.add_argument(
        "--dry-run",
        action="store_true",
        help="dump only: write <incident>.dump.txt files to --out instead of calling a model",
    )
    ev.add_argument("--model", help="model id, e.g. claude-haiku-5-5 (dump and --agent)")
    ev.add_argument(
        "--effort",
        choices=EFFORTS,
        help="always sent; default per model: haiku medium, sonnet high, opus medium, fable high",
    )
    ev.add_argument(
        "--max-usd",
        type=_positive_usd,
        metavar="X",
        help="required for model runs: the run's spending cap in USD. It is checked before "
        "each model call, so a run can overshoot it by at most one call; then exit 5",
    )
    ev.add_argument("--repeats", type=_positive_int, help="runs per incident (default 1)")
    ev.add_argument("--provider", choices=PROVIDERS, help="model provider (default anthropic)")
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


def _usage(message: str) -> int:
    print(f"sickbay: {message}", file=sys.stderr)
    return EXIT_USAGE


def _given_model_flags(args: argparse.Namespace) -> str:
    """The model-run flags given on the command line, e.g. '--model, --repeats'."""
    return ", ".join(
        "--" + f.replace("_", "-") for f in _MODEL_FLAGS if getattr(args, f) is not None
    )


def _out_problem(out: Path) -> str | None:
    """Why a model run cannot write --out and its recording, checked before any call."""
    for path in (out, recording_path(out)):
        if not path.parent.is_dir():
            return f"cannot write {path}: {path.parent} is not a directory"
        if path.is_dir():
            return f"cannot write {path}: it is a directory"
    return None


def _write(path: Path, data: bytes) -> bool:
    try:
        path.write_bytes(data)
    except OSError as exc:
        print(f"sickbay: cannot write {path}: {exc}", file=sys.stderr)
        return False
    return True


def _eval_keyword(args: argparse.Namespace) -> int:
    if flags := _given_model_flags(args):
        return _usage(f"{flags}: for model runs only")
    if args.dry_run:
        return _usage("--dry-run applies to the dump baseline only")
    try:
        result = run_eval(KeywordPredictor(), args.incidents, note=_note)
    except (LabelError, SnapshotError) as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    data = dumps_result(result)
    if args.out is None:
        sys.stdout.flush()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    elif not _write(args.out, data):
        return EXIT_USAGE
    print(summary_line(result), file=sys.stderr)
    return EXIT_OK


def _eval_dump_dry_run(args: argparse.Namespace) -> int:
    if flags := _given_model_flags(args):
        return _usage(f"{flags}: not with --dry-run")
    out: Path | None = args.out
    if out is None:
        return _usage("--dry-run needs --out DIR")
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


def _run_scored(
    predictor: Predictor, args: argparse.Namespace, repeats: int, **run: Any
) -> tuple[dict[str, Any] | None, int]:
    """(result, exit code). result is None when nothing may be written (exit 3).

    ReplayMismatch propagates to the caller.
    """
    try:
        return run_eval(predictor, args.incidents, repeats, note=_note, **run), EXIT_OK
    except (LabelError, SnapshotError) as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return None, EXIT_UNREADABLE
    except RunAborted as exc:
        what = "budget cap hit" if exc.kind == "budget" else "provider error"
        print(f"sickbay: {what}: {exc}", file=sys.stderr)
        return exc.result, EXIT_BUDGET if exc.kind == "budget" else EXIT_PROVIDER


def _eval_model(kind: str, args: argparse.Namespace) -> int:
    """A paid run of the agent or the dump baseline, recorded beside its result."""
    if args.dry_run:
        return _usage("--dry-run applies to the dump baseline only")
    for flag, value in (("--model", args.model), ("--max-usd", args.max_usd), ("--out", args.out)):
        if value is None:
            return _usage(f"{flag} is required for a model run")
    out: Path = args.out
    if problem := _out_problem(out):
        return _usage(problem)
    model: str = args.model
    try:
        price_for(model)
    except UnknownPrice as exc:
        return _usage(str(exc))
    try:
        live = _make_provider(args.provider or PROVIDERS[0])
    except ProviderSetupError as exc:
        return _usage(str(exc))
    effort: str = args.effort or DEFAULT_EFFORT[model]
    repeats: int = args.repeats or 1
    provider = RecordingProvider(live)
    budget = Budget(args.max_usd)
    predictor = MODEL_PREDICTORS[kind](provider, model, effort, budget)
    result, code = _run_scored(
        predictor,
        args,
        repeats,
        provider=live.name,
        max_usd=args.max_usd,
        on_case=provider.start_case,
    )
    if result is None:
        return code
    recording = provider.document(
        result_file=out.name,
        predictor=predictor.name,
        provider=live.name,
        model=model,
        effort=effort,
        prompt_version=predictor.prompt_version,
        max_usd=args.max_usd,
        repeats=repeats,
    )
    # The recording first: a failed result write must not lose the paid exchanges.
    if not _write(recording_path(out), dumps_recording(recording)):
        return EXIT_USAGE
    if not _write(out, dumps_result(result)):
        return EXIT_USAGE
    print(summary_line(result), file=sys.stderr)
    return code


def _eval_replay(args: argparse.Namespace) -> int:
    """Re-run a recorded model run over a ReplayProvider, with no network.

    The budget is the recorded cap: costs are deterministic, so a run that hit its cap
    hits it at the same call again (exit 5). A run stopped by a provider error (exit 4)
    cannot be replayed: its last case has no recorded response.
    """
    if flags := _given_model_flags(args):
        return _usage(f"{flags}: not with --replay (they are read from the recording)")
    if args.dry_run:
        return _usage("--dry-run applies to the dump baseline only")
    if args.out is None:
        return _usage("--out is required for --replay")
    try:
        recording = load_recording(args.replay)
    except RecordingError as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    cls = MODEL_PREDICTORS.get(recording["predictor"])
    if cls is None:
        print(
            f"sickbay: {args.replay}: unknown predictor {recording['predictor']}", file=sys.stderr
        )
        return EXIT_UNREADABLE
    replay = ReplayProvider(recording["cases"])
    budget = Budget(recording["max_usd"])
    try:
        predictor = cls(replay, recording["model"], recording["effort"], budget)
    except UnknownPrice as exc:
        return _usage(str(exc))
    if predictor.prompt_version != recording["prompt_version"]:
        print(
            f"sickbay: replay mismatch: the recording used {recording['prompt_version']}, "
            f"this sickbay has {predictor.prompt_version}",
            file=sys.stderr,
        )
        return EXIT_UNREADABLE
    try:
        result, code = _run_scored(
            predictor,
            args,
            recording["repeats"],
            provider=recording["provider"],
            max_usd=recording["max_usd"],
            on_case=replay.start_case,
        )
        if result is not None and code in (EXIT_OK, EXIT_BUDGET):
            replay.finish()
    except ReplayMismatch as exc:
        print(f"sickbay: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    if result is None:
        return code
    if not _write(args.out, dumps_result(result)):
        return EXIT_USAGE
    print(summary_line(result), file=sys.stderr)
    return code


def _cmd_eval(args: argparse.Namespace) -> int:
    if args.replay is not None:
        return _eval_replay(args)
    if args.agent:
        return _eval_model("agent", args)
    if args.baseline == "dump":
        return _eval_dump_dry_run(args) if args.dry_run else _eval_model("dump", args)
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
