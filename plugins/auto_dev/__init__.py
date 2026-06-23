"""Opt-in, read-only backend automation command."""

from .command import handle_auto_dev


def register(ctx):
    ctx.register_command(
        # Hyphenated to match the gateway dispatch convention: Telegram sends
        # "/auto_dev" and the dispatcher resolves it as "auto-dev"
        # (command.replace("_", "-")), same as the bundled "disk-cleanup" plugin.
        # Registering with an underscore left the command unreachable from Telegram.
        "auto-dev",
        handle_auto_dev,
        description="Inspect or dry-run a backend automation task packet",
        args_hint="status | dry_run <json>",
    )
