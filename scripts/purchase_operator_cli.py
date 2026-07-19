"""Deterministic operator CLI for the Restricted Purchase pilot (Hermes #65).

A small, no-model command surface for Cal/Fable that drives the Cogitator
operator bridge and (for the run step) the root-owned one-shot launch helper.
It NEVER handles payment data, NEVER prints an execution ticket, and NEVER
executes anything on proposal creation.

Flow:
  propose (from a strict JSON file) -> preview -> approve (explicit confirmation
  phrase) -> issue (ticket piped straight to the launch helper, never shown) ->
  status. cancel / revoke are available before execution.

Auth: the Cogitator bridge bearer token comes only from $COGITATOR_BRIDGE_TOKEN.
source_agent is "operator" (role-separated from the executor's "hermes"). The
operator and executor families currently share the one bearer token — a
separately scoped operator token is a future upgrade.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

BRIDGE_PATH = "/api/cogitator_bridge"
TOKEN_ENV = "COGITATOR_BRIDGE_TOKEN"
LAUNCH_HELPER = "/home/v0id/.hermes/hermes-agent/packaging/purchase-executor/launch.sh"


class OperatorError(RuntimeError):
    pass


def _audit(state_dir: str, event: str, **fields) -> None:
    # Full audit receipt of every operator action (never contains a ticket).
    if not state_dir:
        return
    os.makedirs(state_dir, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    path = os.path.join(state_dir, "operator_audit.jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, default=str) + "\n")


def bridge_call(base_url: str, action: str, context: dict, *, user_intent: str) -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise OperatorError(f"{TOKEN_ENV} is not set; refusing (fail closed)")
    packet = {
        "source_agent": "operator",
        "requested_action": action,
        "user_intent": user_intent,
        "context": context,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + BRIDGE_PATH,
        data=json.dumps(packet).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except Exception:
            raise OperatorError(f"bridge HTTP {error.code}") from error
    except (urllib.error.URLError, OSError) as error:
        raise OperatorError(f"bridge transport error: {error}") from error


def _require_ok(result: dict) -> dict:
    status = result.get("status")
    if status == "disabled":
        raise OperatorError(
            "operator bridge is disabled (ENABLE_PURCHASE_OPERATOR_BRIDGE unset)"
        )
    if status != "ok":
        raise OperatorError(
            f"rejected: {result.get('reason_code', 'unknown')}"
            f" ({result.get('field', '')})".rstrip(" ()")
        )
    return result


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# --- subcommands -------------------------------------------------------------


def cmd_propose(args) -> int:
    with open(args.file, encoding="utf-8") as handle:
        proposal = json.load(handle)
    if not isinstance(proposal, dict):
        raise OperatorError("proposal file must be a JSON object")
    result = _require_ok(bridge_call(
        args.bridge_url, "create_purchase_proposal", {"proposal": proposal},
        user_intent="Operator creates one exact purchase proposal.",
    ))
    _audit(args.state_dir, "propose", proposal_id=result.get("proposal_id"))
    _print({"proposal_id": result.get("proposal_id"), "status": "created (NOT approved, NOT executed)"})
    return 0


def cmd_preview(args) -> int:
    result = _require_ok(bridge_call(
        args.bridge_url, "get_purchase_approval_packet", {"proposal_id": args.proposal_id},
        user_intent="Operator previews exact commercial terms before approval.",
    ))
    _print(result)
    return 0


def cmd_status(args) -> int:
    result = _require_ok(bridge_call(
        args.bridge_url, "get_purchase_status", {"proposal_id": args.proposal_id},
        user_intent="Operator reads sanitized purchase status.",
    ))
    _print(result)
    return 0


def cmd_approve(args) -> int:
    # Explicit confirmation phrase must contain the proposal id, exact maximum,
    # and currency. No default yes. Non-interactive still requires the phrase.
    expected = f"approve {args.proposal_id} {args.maximum} {args.currency}"
    phrase = args.confirm if args.confirm is not None else (
        "" if args.non_interactive else input(
            f"Type EXACTLY to approve (no default): {expected}\n> "
        )
    )
    if phrase.strip() != expected:
        raise OperatorError(
            "confirmation phrase did not match exactly; approval aborted "
            f"(expected: {expected!r})"
        )
    result = _require_ok(bridge_call(
        args.bridge_url, "approve_and_reserve_purchase",
        {
            "proposal_id": args.proposal_id,
            "approved_maximum": args.maximum,
            "idempotency_key": args.idempotency_key,
            "confirm": True,
        },
        user_intent="Operator approves exact terms and reserves budget.",
    ))
    _audit(args.state_dir, "approve", proposal_id=args.proposal_id,
           maximum=args.maximum, currency=args.currency)
    _print({"proposal_id": args.proposal_id, "status": "approved + reserved (execution is a SEPARATE step)"})
    return 0


def cmd_cancel(args) -> int:
    result = _require_ok(bridge_call(
        args.bridge_url, "cancel_purchase_before_execution",
        {"proposal_id": args.proposal_id, "idempotency_key": args.idempotency_key},
        user_intent="Operator cancels a proposal before execution.",
    ))
    _audit(args.state_dir, "cancel", proposal_id=args.proposal_id)
    _print({"proposal_id": args.proposal_id, "status": "cancelled"})
    return 0


def cmd_revoke(args) -> int:
    result = _require_ok(bridge_call(
        args.bridge_url, "revoke_unexecuted_approval",
        {"proposal_id": args.proposal_id, "idempotency_key": args.idempotency_key},
        user_intent="Operator revokes an unexecuted approval and releases the reservation.",
    ))
    _audit(args.state_dir, "revoke", proposal_id=args.proposal_id)
    _print({"proposal_id": args.proposal_id, "status": "approval revoked, reservation released"})
    return 0


def cmd_issue(args) -> int:
    # Issues one ticket and immediately hands it off WITHOUT ever printing it.
    # --launch pipes it straight into the root launch helper; otherwise it is
    # written to a private 0600 file whose PATH (not contents) is reported.
    if not args.launch and not args.out:
        raise OperatorError("issue requires --launch (pipe to helper) or --out <path>")
    result = _require_ok(bridge_call(
        args.bridge_url, "issue_execution_ticket",
        {"proposal_id": args.proposal_id, "idempotency_key": args.idempotency_key},
        user_intent="Operator issues one execution ticket.",
    ))
    ticket = result.get("ticket_token", "")
    if not ticket:
        raise OperatorError("bridge did not return a ticket token")
    _audit(args.state_dir, "issue", proposal_id=args.proposal_id,
           ticket_id=result.get("ticket_id"))
    if args.launch:
        proc = subprocess.run(
            ["sudo", LAUNCH_HELPER], input=ticket, text=True,
        )
        del ticket
        _audit(args.state_dir, "launch", proposal_id=args.proposal_id, rc=proc.returncode)
        print(f"launched (helper rc={proc.returncode}); ticket never printed. "
              f"Check: journalctl -u hermes-purchase-executor -n 50")
        return proc.returncode
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(ticket)
    del ticket
    print(f"ticket written 0600 to {args.out} (contents not shown). "
          f"Pipe it to the launch helper, then delete it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restricted purchase operator CLI (no model, no payment data).")
    parser.add_argument("--bridge-url", default=os.environ.get("COGITATOR_BRIDGE_URL", ""),
                        help="Cogitator bridge base URL (or $COGITATOR_BRIDGE_URL)")
    parser.add_argument("--state-dir", default=os.path.expanduser("~/.hermes/purchase_operator"),
                        help="operator audit directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("propose"); p.add_argument("--file", required=True); p.set_defaults(func=cmd_propose)
    p = sub.add_parser("preview"); p.add_argument("proposal_id"); p.set_defaults(func=cmd_preview)
    p = sub.add_parser("status"); p.add_argument("proposal_id"); p.set_defaults(func=cmd_status)

    p = sub.add_parser("approve")
    p.add_argument("proposal_id")
    p.add_argument("--maximum", required=True)
    p.add_argument("--currency", required=True)
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--confirm", default=None, help="exact confirmation phrase (non-interactive)")
    p.add_argument("--non-interactive", action="store_true")
    p.set_defaults(func=cmd_approve)

    for name, func in (("cancel", cmd_cancel), ("revoke", cmd_revoke)):
        p = sub.add_parser(name)
        p.add_argument("proposal_id")
        p.add_argument("--idempotency-key", required=True)
        p.set_defaults(func=func)

    p = sub.add_parser("issue")
    p.add_argument("proposal_id")
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--launch", action="store_true", help="pipe the ticket straight to the root launch helper")
    p.add_argument("--out", default="", help="write the ticket to this 0600 path instead")
    p.set_defaults(func=cmd_issue)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.bridge_url:
        print("error: --bridge-url or $COGITATOR_BRIDGE_URL required", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except OperatorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
