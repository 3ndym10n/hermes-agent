#!/usr/bin/env python3
"""Read and operate the local governed-commerce job state machine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from commerce_jobs import (  # noqa: E402
    CommerceForbiddenDataError,
    CommerceInvalidTransitionError,
    CommerceJobError,
    CommerceJobStore,
    CommerceNotFoundError,
    CommerceStaleVersionError,
    default_db_path,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 3
EXIT_INVALID_TRANSITION = 4
EXIT_STALE_VERSION = 5
EXIT_FORBIDDEN_DATA = 6


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or control Hermes commerce jobs; performs no provider work."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=default_db_path(),
        help="Commerce SQLite database path.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List commerce jobs.")
    status = commands.add_parser("status", help="Show one commerce job.")
    status.add_argument("job_id")
    pause = commands.add_parser("pause", help="Pause a nonterminal job.")
    pause.add_argument("job_id")
    pause.add_argument("--reason", required=True)
    resume = commands.add_parser("resume", help="Resume a paused job.")
    resume.add_argument("job_id")
    cancel = commands.add_parser("cancel", help="Cancel a safe nonterminal job.")
    cancel.add_argument("job_id")
    cancel.add_argument("--reason", required=True)
    return parser


def _exit_code(error: CommerceJobError) -> int:
    if isinstance(error, CommerceNotFoundError):
        return EXIT_NOT_FOUND
    if isinstance(error, CommerceInvalidTransitionError):
        return EXIT_INVALID_TRANSITION
    if isinstance(error, CommerceStaleVersionError):
        return EXIT_STALE_VERSION
    if isinstance(error, CommerceForbiddenDataError):
        return EXIT_FORBIDDEN_DATA
    return EXIT_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = CommerceJobStore(args.db)
    try:
        if args.command == "list":
            result: object = {"jobs": store.list_jobs()}
        elif args.command == "status":
            result = {"job": store.get_job(args.job_id)}
        else:
            job = store.get_job(args.job_id)
            version = int(job["row_version"])
            if args.command == "pause":
                updated = store.pause(
                    args.job_id,
                    expected_version=version,
                    actor="operator_cli",
                    reason=args.reason,
                )
            elif args.command == "resume":
                updated = store.resume(
                    args.job_id,
                    expected_version=version,
                    actor="operator_cli",
                )
            elif args.command == "cancel":
                updated = store.cancel(
                    args.job_id,
                    expected_version=version,
                    actor="operator_cli",
                    reason=args.reason,
                )
            else:
                raise AssertionError("unreachable")
            result = {"job": updated}
        print(_json({"ok": True, "result": result}))
        return EXIT_OK
    except CommerceJobError as error:
        print(
            _json({
                "ok": False,
                "error": {"code": error.code, "field": error.field},
            }),
            file=sys.stderr,
        )
        return _exit_code(error)


if __name__ == "__main__":
    raise SystemExit(main())
