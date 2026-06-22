"""Opt-in, read-only backend automation command."""

from .command import handle_auto_dev


def register(ctx):
    ctx.register_command(
        "auto_dev",
        handle_auto_dev,
        description="Inspect or dry-run a backend automation task packet",
        args_hint="status | dry_run <json>",
    )
