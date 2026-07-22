"""Security-focused tests for deterministic checkout discovery (issue #73)."""

import json

import pytest  # ty: ignore[unresolved-import]

import purchase_discovery as discovery


MERCHANT = "merchant.example"
PAGE_ORIGIN = f"https://{MERCHANT}"
PROCESSOR_ORIGIN = "https://pay.processor.example"
AUTOCOMPLETE = {
    "card_number": "cc-number",
    "card_expiry": "cc-exp",
    "card_cvv": "cc-csc",
    "card_name": "cc-name",
}


def field(logical, *, selector=None, autocomplete=None, label=None, name=None,
          form_key="form#payment", action=f"{PAGE_ORIGIN}/pay",
          visible=True, enabled=True, readonly=False):
    return {
        "selector": selector or f"input#{logical}",
        "role": "textbox",
        "type": "text",
        "name": name or logical,
        "id": "",
        "autocomplete": AUTOCOMPLETE[logical] if autocomplete is None else autocomplete,
        "accessible_name": label if label is not None else logical.replace("_", " "),
        "visible": visible,
        "enabled": enabled,
        "readonly": readonly,
        "form_key": form_key,
        "form_action": action,
        "form_context": True,
    }


def submit(*, selector="button#pay", action=f"{PAGE_ORIGIN}/pay", name="Pay now",
           form_key="form#payment", form_context=True):
    return {
        "selector": selector,
        "role": "button",
        "type": "submit",
        "name": "",
        "id": "",
        "autocomplete": "",
        "accessible_name": name,
        "visible": True,
        "enabled": True,
        "readonly": False,
        "form_key": form_key,
        "form_action": action,
        "form_context": form_context,
    }


def context(url, *, fields=(), submits=(), frames=(), challenge=False):
    return {
        "url": url,
        "fields": list(fields),
        "submits": list(submits),
        "frames": list(frames),
        "challenge": challenge,
    }


def inspector(mapping):
    return lambda frame_path: mapping.get(
        frame_path, {"success": False, "error": "missing frame"}
    )


def discover(mapping, **kwargs):
    return discovery.discover_checkout(
        inspector(mapping),
        canonical_domain=MERCHANT,
        **kwargs,
    )


def same_page_fields(**changes):
    result = []
    for logical in discovery.FIELD_NAMES:
        values = dict(changes.get(logical, {}))
        result.append(field(logical, **values))
    return result


def test_same_page_semantics_are_unique_and_audit_is_sanitized():
    plan = discover({
        (): {"success": True, "result": context(
            f"{PAGE_ORIGIN}/checkout",
            fields=same_page_fields(),
            submits=[submit()],
        )}
    })
    assert [match.field for match in plan.fields] == list(discovery.FIELD_NAMES)
    assert all(match.confidence == 100 for match in plan.fields)
    assert plan.submit.command("click") == [
        "find", "role", "button", "click", "--name", "Pay now", "--exact"
    ]
    audit = plan.audit()
    assert audit["page_origin"] == PAGE_ORIGIN
    assert "selector" not in str(audit)
    assert "locator_value" not in str(audit)
    assert "value" not in str(audit).lower()


def test_audit_metadata_is_canonical_and_drops_arbitrary_dom_strings():
    hostile = "acct-Cal-4111111111111111-session-token"
    fields = same_page_fields()
    for item in fields:
        item["name"] = hostile
        item["id"] = hostile
        item["accessible_name"] = hostile
    plan = discover({
        (): {"success": True, "result": context(
            f"{PAGE_ORIGIN}/checkout",
            fields=fields,
            submits=[submit(name="Pay now")],
        )}
    })
    serialized = json.dumps(plan.audit())
    assert hostile not in serialized
    assert "4111111111111111" not in serialized
    for field_audit in plan.audit()["fields"]:
        assert set(field_audit) == {
            "field", "frame_origin", "role", "type",
            "name_attribute_present", "id_attribute_present", "autocomplete",
            "match_basis", "confidence", "form_action_origin",
        }


def test_accessible_label_and_standard_identifier_are_supported():
    fields = same_page_fields(
        card_expiry={"autocomplete": "", "label": "Expiration date", "name": "changed"},
        card_cvv={"autocomplete": "", "label": "", "name": "security-code"},
        card_name={"autocomplete": "", "label": "Name on card", "name": "changed"},
    )
    plan = discover({
        (): {"success": True, "result": context(
            f"{PAGE_ORIGIN}/checkout", fields=fields, submits=[submit()]
        )}
    })
    expiry, cvv, name = plan.fields[1:]
    assert expiry.command("fill", "synthetic")[:3] == ["find", "label", "Expiration date"]
    assert cvv.confidence == 90 and cvv.locator_kind == "css"
    assert name.confidence == 95 and name.locator_kind == "label"


def test_exact_allowlisted_hosted_frame_is_traversed():
    frame_selector = "iframe#hosted-card"
    main = context(
        f"{PAGE_ORIGIN}/checkout",
        submits=[submit(form_context=True)],
        frames=[{
            "selector": frame_selector,
            "src": f"{PROCESSOR_ORIGIN}/fields",
            "hint": "Secure payment fields",
            "visible": True,
        }],
    )
    hosted = context(
        f"{PROCESSOR_ORIGIN}/fields",
        fields=[
            field(
                logical,
                action=f"{PROCESSOR_ORIGIN}/tokenize",
                form_key="form#hosted",
            )
            for logical in discovery.FIELD_NAMES
        ],
    )
    plan = discover(
        {
            (): {"success": True, "result": main},
            (frame_selector,): {"success": True, "result": hosted},
        },
        processor_origins=(PROCESSOR_ORIGIN,),
    )
    assert plan.frame_origins == (PROCESSOR_ORIGIN,)
    assert all(match.frame_origin == PROCESSOR_ORIGIN for match in plan.fields)
    assert plan.submit.frame_origin == PAGE_ORIGIN


@pytest.mark.parametrize(
    "extra_fields,extra_frames",
    [
        (
            [field(
                "card_cvv",
                selector="input#otp",
                autocomplete="one-time-code",
                label="Code",
                name="otp",
                action=f"{PROCESSOR_ORIGIN}/authenticate",
            )],
            [],
        ),
        (
            [],
            [{
                "selector": "iframe#captcha",
                "src": "https://www.google.com/recaptcha/api2/anchor",
                "hint": "reCAPTCHA",
                "visible": True,
            }],
        ),
    ],
    ids=["metadata-only-mfa", "captcha-iframe"],
)
def test_hosted_challenge_metadata_fails_closed(extra_fields, extra_frames):
    frame_selector = "iframe#hosted-card"
    main = context(
        f"{PAGE_ORIGIN}/checkout",
        submits=[submit()],
        frames=[{
            "selector": frame_selector,
            "src": f"{PROCESSOR_ORIGIN}/fields",
            "hint": "Secure payment fields",
            "visible": True,
        }],
    )
    hosted = context(
        f"{PROCESSOR_ORIGIN}/fields",
        fields=[
            field(logical, action=f"{PROCESSOR_ORIGIN}/tokenize")
            for logical in discovery.FIELD_NAMES
        ] + extra_fields,
        frames=extra_frames,
    )
    with pytest.raises(discovery.DiscoveryError) as error:
        discover(
            {
                (): {"success": True, "result": main},
                (frame_selector,): {"success": True, "result": hosted},
            },
            processor_origins=(PROCESSOR_ORIGIN,),
        )
    assert error.value.category == "human_challenge_required"
    assert error.value.reason == "hosted_challenge_detected"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (
            lambda fields: fields + [
                field("card_number", selector="input#decoy", label="Card number")
            ],
            "card_number_ambiguous",
        ),
        (
            lambda fields: [
                dict(item, visible=False) if item["autocomplete"] == "cc-csc" else item
                for item in fields
            ],
            "card_cvv_missing",
        ),
    ],
)
def test_ambiguous_or_hidden_fields_fail_closed(mutation, reason):
    with pytest.raises(discovery.DiscoveryError) as error:
        discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                fields=mutation(same_page_fields()),
                submits=[submit()],
            )}
        })
    assert error.value.category == "checkout_not_ready"
    assert error.value.reason == reason


def test_malicious_unrelated_payment_form_causes_ambiguity():
    decoy = field(
        "card_number",
        selector="form#newsletter input",
        form_key="form#newsletter",
        action="https://evil.example/collect",
    )
    plan = discover({
        (): {"success": True, "result": context(
            f"{PAGE_ORIGIN}/checkout",
            fields=same_page_fields() + [decoy],
            submits=[submit()],
        )}
    })
    # The wrong-action decoy is ineligible; the real checkout remains valid.
    assert plan.fields[0].form_action_origin == PAGE_ORIGIN
    # If the malicious form posts to an allowed origin it becomes ambiguous.
    decoy["form_action"] = f"{PAGE_ORIGIN}/collect"
    with pytest.raises(discovery.DiscoveryError) as error:
        discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                fields=same_page_fields() + [decoy],
                submits=[submit()],
            )}
        })
    assert error.value.reason == "card_number_ambiguous"


def test_payment_frame_on_wrong_origin_is_rejected_without_traversal():
    with pytest.raises(discovery.DiscoveryError) as error:
        discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                submits=[submit()],
                frames=[{
                    "selector": "iframe#evil",
                    "src": "https://evil.example/card",
                    "hint": "Secure card payment",
                    "visible": True,
                }],
            )}
        })
    assert error.value.category == "wrong_origin"
    assert error.value.reason == "payment_frame_origin_rejected"


def test_blank_hint_wrong_origin_processor_src_is_rejected():
    with pytest.raises(discovery.DiscoveryError) as error:
        discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                submits=[submit()],
                frames=[{
                    "selector": "iframe#processor",
                    "src": "https://js.stripe.com/v3/fields",
                    "hint": "",
                    "visible": True,
                }],
            )}
        })
    assert error.value.category == "wrong_origin"
    assert error.value.reason == "payment_frame_origin_rejected"


def test_inaccessible_allowlisted_frame_is_checkout_not_ready():
    with pytest.raises(discovery.DiscoveryError) as error:
        discover(
            {
                (): {"success": True, "result": context(
                    f"{PAGE_ORIGIN}/checkout",
                    submits=[submit()],
                    frames=[{
                        "selector": "iframe#processor",
                        "src": f"{PROCESSOR_ORIGIN}/fields",
                        "hint": "Secure payment fields",
                        "visible": True,
                    }],
                )}
            },
            processor_origins=(PROCESSOR_ORIGIN,),
        )
    assert error.value.category == "checkout_not_ready"
    assert error.value.reason == "frame_inaccessible"


def test_duplicate_submit_controls_fail_closed():
    with pytest.raises(discovery.DiscoveryError) as error:
        discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                fields=same_page_fields(),
                submits=[submit(), submit(selector="button#pay-again")],
            )}
        })
    assert error.value.reason == "submit_ambiguous"


def test_semantic_dom_change_keeps_same_plan():
    def plan(prefix):
        return discover({
            (): {"success": True, "result": context(
                f"{PAGE_ORIGIN}/checkout",
                fields=[
                    field(logical, selector=f"{prefix} > input:nth-of-type({index})")
                    for index, logical in enumerate(discovery.FIELD_NAMES, 1)
                ],
                submits=[submit(selector=f"{prefix} > button")],
            )}
        })

    assert plan("section#old").fingerprint == plan("main#new").fingerprint


@pytest.mark.parametrize(
    "origins",
    [
        ("https://*.processor.example",),
        ("http://pay.processor.example",),
        ("https://pay.processor.example/path",),
    ],
)
def test_processor_allowlist_rejects_wildcard_non_https_or_non_origin(origins):
    with pytest.raises(ValueError):
        discover({
            (): {"success": True, "result": context(f"{PAGE_ORIGIN}/checkout")}
        }, processor_origins=origins)
