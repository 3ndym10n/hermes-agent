"""Assembly layer for the commerce purchase-governance client.

The gateway coordinates approved decisions but must not own or import
purchase-executor machinery (``tests/test_purchase_executor.py::
test_gateway_never_imports_executor``). Bridge-client construction therefore
lives here, outside ``gateway/``, and the built object is injected into
whichever surface needs it -- the worker or the gateway watcher.
"""

from __future__ import annotations

import os

from gateway.cogitator_intake_bridge import TOKEN_ENV
from gateway.commerce_buttons import CommercePurchaseGovernance
from hermes_cli.config import cfg_get, load_config_readonly


def build_purchase_governance(
    *, bridge_url: str, bridge_token: str
) -> CommercePurchaseGovernance:
    """Wire the role-separated bridge clients into one governance adapter.

    The operator and executor roles stay distinct: proposals and approvals go
    out over the operator call, ticket claims and completion reports over the
    executor call.
    """

    from purchase_executor import bridge_post_factory
    from scripts.purchase_operator_cli import bridge_call

    return CommercePurchaseGovernance(
        bridge_url=bridge_url,
        operator_call=bridge_call,
        executor_call=bridge_post_factory(bridge_url, bridge_token),
    )


def purchase_governance_from_config() -> CommercePurchaseGovernance | None:
    """Build governance from the deployed config, or None if unconfigured.

    Returning None rather than raising lets each caller decide: the worker
    starts without a money bridge, the gateway refuses the approval.
    """

    bridge_url = str(
        cfg_get(load_config_readonly(), "intake", "base_url", default="") or ""
    ).strip()
    bridge_token = str(os.environ.get(TOKEN_ENV, "") or "").strip()
    if not bridge_url or not bridge_token:
        return None
    return build_purchase_governance(bridge_url=bridge_url, bridge_token=bridge_token)


__all__ = ["build_purchase_governance", "purchase_governance_from_config"]
