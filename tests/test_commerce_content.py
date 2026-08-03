import copy

import pytest

from commerce_content import (
    FORBIDDEN_CLAIM_TERMS,
    ContentBuildError,
    build_content,
    canonical_content_bytes,
    content_fingerprint,
    scan_forbidden_claims,
)


@pytest.fixture
def facts():
    return {
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Warp Supply is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    }


def test_content_is_claim_free_escaped_and_byte_stable(facts):
    facts["business_identity_sentence"] = (
        "Example Trading & its owner operate Warp Supply."
    )
    first = build_content(facts)
    second = build_content(copy.deepcopy(facts))

    assert first == second
    assert canonical_content_bytes(first) == canonical_content_bytes(second)
    assert content_fingerprint(first) == content_fingerprint(second)
    assert len(content_fingerprint(first)) == 64
    body = first["pages"]["priority-access"]["body_html"]
    assert "Example Trading &amp; its owner" in body
    assert "checked" not in body
    assert scan_forbidden_claims(body) == ()
    assert first["privacy_policy"] == {"source": "shopify_generated", "reviewed": True}


@pytest.mark.parametrize("claim", FORBIDDEN_CLAIM_TERMS)
def test_every_mandate_forbidden_claim_is_rejected(facts, claim):
    facts["business_identity_sentence"] = f"Example Trading states {claim}."
    with pytest.raises(ContentBuildError, match="forbidden launch claim"):
        build_content(facts)


@pytest.mark.parametrize(
    "claim",
    (
        "The final price is AUD 999.00.",
        "The card is in stock.",
        "We guarantee allocation.",
        "Ships within two days.",
        "Official AMD inventory.",
        "Benchmarked at 144 fps.",
    ),
)
def test_equivalent_ungrounded_claims_are_rejected(facts, claim):
    facts["business_identity_sentence"] = claim
    with pytest.raises(ContentBuildError, match="forbidden launch claim"):
        build_content(facts)


def test_missing_signoff_extra_fact_and_placeholder_fail_closed(facts):
    facts["privacy_signoff"] = False
    with pytest.raises(ContentBuildError, match="sign-off"):
        build_content(facts)

    facts["privacy_signoff"] = True
    facts["supplier"] = "unknown"
    with pytest.raises(ContentBuildError, match="exactly"):
        build_content(facts)

    facts.pop("supplier")
    facts["business_identity_sentence"] = "⟨business identity sentence⟩"
    with pytest.raises(ContentBuildError, match="placeholder"):
        build_content(facts)
