import copy
import json
from pathlib import Path

import pytest

from commerce_content import build_content
from commerce_verify import (
    HTTPResult,
    VerificationConfigurationError,
    verify_launch,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "shopify_admin" / "verification_green.json"
)
DOMAIN = "warpsupply.com"


def _content():
    return build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })["pages"]["priority-access"]["body_html"]


def _green():
    fixture = json.loads(FIXTURE.read_text())
    approved = _content()
    root = f"<html><body>{approved}</body></html>".encode()
    responses = {
        f"https://{DOMAIN}/": HTTPResult(200, root, f"https://{DOMAIN}/"),
        f"https://www.{DOMAIN}/": HTTPResult(
            200,
            root,
            f"https://{DOMAIN}/",
            redirects=(f"https://{DOMAIN}/",),
        ),
        f"https://{DOMAIN}/policies/privacy-policy": HTTPResult(
            200, b"<h1>Privacy Policy</h1>", f"https://{DOMAIN}/policies/privacy-policy"
        ),
        f"https://{DOMAIN}/pages/contact": HTTPResult(
            200, b"<h1>Contact</h1>", f"https://{DOMAIN}/pages/contact"
        ),
        f"https://{DOMAIN}/cart": HTTPResult(
            200, b"<h1>Your cart is empty</h1>", f"https://{DOMAIN}/cart"
        ),
        f"https://{DOMAIN}/checkout": HTTPResult(
            200,
            b"<h1>Your cart is empty</h1>",
            f"https://{DOMAIN}/cart",
            redirects=(f"https://{DOMAIN}/cart",),
        ),
        f"https://{DOMAIN}/products/__virgil_missing__": HTTPResult(
            404, b"not found", f"https://{DOMAIN}/products/__virgil_missing__"
        ),
    }

    def fetch(url):
        return responses[url]

    def dns(host, record_type):
        values = fixture["expected_dns"][record_type]
        return {"resolver-a": values, "resolver-b": values}

    arguments = {
        "domain": DOMAIN,
        "approved_html": approved,
        "fetch": fetch,
        "dns_lookup": dns,
        "expected_dns": fixture["expected_dns"],
        "registrar_active": True,
        "waitlist_probe": fixture["waitlist_probe"],
        "mobile_screenshot_ok": True,
        "products_count": 0,
    }
    return arguments, responses


def test_reserved_cart_200_is_green_only_with_empty_cart_truth_and_no_commerce_controls():
    arguments, _ = _green()
    report = verify_launch(**arguments)

    assert report["all_green"] is True
    assert report["checkout_absent_verified"] is True
    assert report["no_payment_collected"] is True
    serialized = json.dumps(report, sort_keys=True)
    assert "@" not in serialized
    assert "launch@example.test" not in serialized


@pytest.mark.parametrize(
    ("case", "failed_check"),
    (
        ("ssl", "public_ssl_and_redirects"),
        ("content", "approved_content"),
        ("placeholder", "placeholders"),
        ("mobile", "mobile_viewport"),
        ("waitlist", "waitlist"),
        ("link", "links"),
        ("commerce", "checkout_absent"),
        ("dns", "dns"),
        ("registrar", "registrar"),
        ("claim", "forbidden_claims"),
    ),
)
def test_each_check_has_a_red_case(case, failed_check):
    arguments, responses = _green()
    root_url = f"https://{DOMAIN}/"
    if case == "ssl":
        previous = responses[f"https://www.{DOMAIN}/"]
        responses[f"https://www.{DOMAIN}/"] = HTTPResult(
            previous.status,
            previous.body,
            previous.final_url,
            previous.redirects,
            tls_valid=False,
        )
    elif case == "content":
        arguments["approved_html"] = "different approved bytes"
    elif case in {"placeholder", "claim", "commerce"}:
        marker = {
            "placeholder": "<p>⟨contact email⟩</p>",
            "claim": "<p>warranty</p>",
            "commerce": '<form action="/cart"><button>Add to cart</button></form>',
        }[case]
        previous = responses[root_url]
        responses[root_url] = HTTPResult(
            previous.status,
            previous.body + marker.encode(),
            previous.final_url,
        )
    elif case == "mobile":
        arguments["mobile_screenshot_ok"] = False
    elif case == "waitlist":
        arguments["waitlist_probe"] = {
            **arguments["waitlist_probe"],
            "consent_recorded": False,
        }
    elif case == "link":
        responses[f"https://{DOMAIN}/pages/contact"] = HTTPResult(
            500, b"failed", f"https://{DOMAIN}/pages/contact"
        )
    elif case == "dns":
        expected = arguments["expected_dns"]

        def bad_dns(host, record_type):
            values = expected[record_type]
            return {"resolver-a": values, "resolver-b": ["wrong.example"]}

        arguments["dns_lookup"] = bad_dns
    elif case == "registrar":
        arguments["registrar_active"] = False

    report = verify_launch(**arguments)
    checks = {check["name"]: check["passed"] for check in report["checks"]}
    assert report["all_green"] is False
    assert checks[failed_check] is False


def test_nonempty_reserved_cart_is_red():
    arguments, responses = _green()
    responses[f"https://{DOMAIN}/cart"] = HTTPResult(
        200,
        b'<form action="/checkout"><button>Checkout</button></form>',
        f"https://{DOMAIN}/cart",
    )
    report = verify_launch(**arguments)
    assert report["checkout_absent_verified"] is False


def test_preorder_control_is_red():
    arguments, responses = _green()
    root_url = f"https://{DOMAIN}/"
    previous = responses[root_url]
    responses[root_url] = HTTPResult(
        previous.status,
        previous.body + b"<button>Pre-order</button>",
        previous.final_url,
    )
    report = verify_launch(**arguments)
    assert report["checkout_absent_verified"] is False


def test_verifier_rejects_off_host_redirect_and_pii_probe_shape():
    arguments, responses = _green()
    responses[f"https://www.{DOMAIN}/"] = HTTPResult(
        200,
        b"ok",
        "https://evil.test/",
        redirects=("https://evil.test/",),
    )
    with pytest.raises(VerificationConfigurationError, match="pinned"):
        verify_launch(**arguments)

    arguments, _ = _green()
    arguments["waitlist_probe"] = {
        **copy.deepcopy(arguments["waitlist_probe"]),
        "email": "waitlist-test@example.test",
    }
    with pytest.raises(VerificationConfigurationError, match="shape"):
        verify_launch(**arguments)


def test_canonical_check_requires_www_redirect_and_stable_apex():
    arguments, responses = _green()
    www_url = f"https://www.{DOMAIN}/"
    previous = responses[www_url]
    responses[www_url] = HTTPResult(
        previous.status, previous.body, previous.final_url, redirects=()
    )
    first = verify_launch(**arguments)

    arguments, responses = _green()
    root_url = f"https://{DOMAIN}/"
    previous = responses[root_url]
    responses[root_url] = HTTPResult(
        previous.status,
        previous.body,
        previous.final_url,
        redirects=(previous.final_url,),
    )
    second = verify_launch(**arguments)

    for report in (first, second):
        checks = {check["name"]: check["passed"] for check in report["checks"]}
        assert checks["public_ssl_and_redirects"] is False


def test_verifier_rejects_malformed_http_result_fields():
    arguments, responses = _green()
    root_url = f"https://{DOMAIN}/"
    responses[root_url] = HTTPResult(200, "not bytes", root_url)
    with pytest.raises(VerificationConfigurationError, match="HTTPResult"):
        verify_launch(**arguments)

    arguments, _ = _green()
    arguments["domain"] = "bad.-host.example"
    with pytest.raises(VerificationConfigurationError, match="domain"):
        verify_launch(**arguments)


class _LedgerStub:
    """Only the ledger surface production_verify reads."""

    def __init__(self, actions):
        self._actions = actions

    def list_actions(self, _job_id):
        return list(self._actions)


def _registration_action(**result_overrides):
    result = {
        "domain": DOMAIN,
        "order_id": "1234",
        "amount_usd_cents": 1198,
        "cogitator": {
            "proposal_id": "pp_1",
            "approval_id": "pa_1",
            "receipt_ref": "purchase-receipts/domain-1",
        },
    }
    result.update(result_overrides)
    return {
        "action_type": "porkbun_register_domain",
        "action_status": "succeeded",
        "result": result,
    }


class _ShopStub:
    def __init__(self, password_protected=True, products=0):
        self._locked = password_protected
        self._products = products

    def commerce_surface(self):
        return {
            "products_count": self._products,
            "supported_digital_wallets": ("SHOPIFY_PAY",),
        }

    def storefront_probe(self, _path="/"):
        return {"status": 200, "password_protected": self._locked}


def _production_job():
    return {
        "job_id": "cj_verify_01",
        "plan": {
            "domain": DOMAIN,
            "shopify": {
                "shop_id": "gid://shopify/Shop/1",
                "myshopify_domain": "warp-supply.myshopify.com",
                "plan": "Basic",
            },
        },
    }


def _production_verify(actions, **overrides):
    from commerce_verify import production_verify

    arguments, _responses = _green()
    return production_verify(
        store=_LedgerStub(actions),
        porkbun_factory=lambda: type(
            "P", (), {"list_domains": lambda _self: {"domains": [{"domain": DOMAIN}]}}
        )(),
        waitlist_probe=lambda *_a: {
            **json.loads(FIXTURE.read_text())["waitlist_probe"],
            "test_address_digest": "sha256:" + "a" * 64,
        },
        mobile_screenshot=lambda *_a: True,
        fetch=arguments["fetch"],
        dns_lookup=arguments["dns_lookup"],
        **overrides,
    )


def test_production_verify_builds_receipt_facts_from_the_ledger():
    verify = _production_verify([_registration_action()])
    package = build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })

    report = verify(_production_job(), _ShopStub(), package, "final")

    assert report["all_green"] is True
    facts = report["receipt_facts"]
    assert facts["domain"]["order_id"] == "1234"
    assert facts["domain"]["spend"] == {
        "amount_usd_cents": 1198,
        "display": "US$11.98",
    }
    assert facts["domain"]["cogitator"]["receipt_ref"] == "purchase-receipts/domain-1"
    assert facts["public_url"] == f"https://{DOMAIN}/"
    assert facts["no_payment_collected"] is True
    assert facts["checkout_absent_verified"] is True
    assert facts["waitlist_test"]["test_subscriber_deleted"] is True


def test_production_verify_prepublish_greens_through_the_storefront_lock():
    """Pre-publication the public checklist cannot pass; this one must."""
    verify = _production_verify([_registration_action()])
    package = build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })

    report = verify(_production_job(), _ShopStub(), package, "prepublish")

    assert report["all_green"] is True
    assert report["checklist"] == "9.3-prepublish"
    assert report["checkout_absent_verified"] is True
    assert "receipt_facts" not in report
    assert {check["name"] for check in report["checks"]} == {
        "storefront_locked",
        "no_products",
        "dns",
    }


def test_production_verify_prepublish_is_red_if_the_store_is_already_public():
    verify = _production_verify([_registration_action()])
    package = build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })

    report = verify(
        _production_job(), _ShopStub(password_protected=False), package, "prepublish"
    )

    assert report["all_green"] is False


def test_production_verify_prepublish_is_red_with_a_sellable_product():
    verify = _production_verify([_registration_action()])
    package = build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })

    report = verify(
        _production_job(), _ShopStub(products=1), package, "prepublish"
    )

    assert report["all_green"] is False
    assert report["checkout_absent_verified"] is False


def test_expected_dns_matches_the_workflow_bundle():
    """The verifier and the DNS writer must not drift apart."""
    from commerce_verify import EXPECTED_SHOPIFY_DNS
    from commerce_workflow import SHOPIFY_DNS_BUNDLE

    bundle = {
        record["type"]: [record["content"]] for record in SHOPIFY_DNS_BUNDLE
    }
    assert {
        key: [value.rstrip(".").lower() for value in values]
        for key, values in EXPECTED_SHOPIFY_DNS.items()
    } == {
        key: [value.rstrip(".").lower() for value in values]
        for key, values in bundle.items()
    }


def test_production_verify_refuses_a_registration_the_money_authority_never_saw():
    action = _registration_action()
    del action["result"]["cogitator"]
    verify = _production_verify([action])
    package = build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })

    with pytest.raises(VerificationConfigurationError):
        verify(_production_job(), _ShopStub(), package, "final")
