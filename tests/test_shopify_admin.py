import json
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from commerce_content import build_content
from shopify_admin import (
    API_VERSION,
    MAX_RESPONSE_BYTES,
    SHOPIFY_SCOPES,
    TOKEN_EXPIRES_IN,
    ShopifyAdminClient,
    ShopifyAuthorizationError,
    ShopifyConfigurationError,
    ShopifyResponseError,
    ShopifyTransportError,
    ShopifyUnsupportedError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shopify_admin"
TOKEN = "test-token-never-persist"


class FakeShopify:
    def __init__(self):
        self.identity = json.loads((FIXTURES / "shop_identity.json").read_text())
        self.customer_email = "waitlist-test@example.test"
        self.customer_deleted = False
        self.pages = {}
        self.menus = {}
        self.theme_settings = "{}"
        self.mutations = []
        self.operations = []

    def __call__(self, request, timeout, limit):
        assert request.full_url == (
            "https://silicon-current.myshopify.com/admin/api/"
            f"{API_VERSION}/graphql.json"
        )
        assert request.method == "POST"
        assert timeout > 0
        assert limit == MAX_RESPONSE_BYTES
        assert request.get_header("X-shopify-access-token") == TOKEN
        document = json.loads(request.data)
        operation = document["operationName"]
        variables = document["variables"]
        self.operations.append(operation)

        if operation == "VirgilShopIdentity":
            return self._reply(self.identity)
        if operation == "VirgilPages":
            handle = variables["query"].split(":", 1)[1]
            nodes = [self.pages[handle]] if handle in self.pages else []
            return self._reply({"data": {"pages": {"nodes": nodes}}})
        if operation in {"VirgilPageCreate", "VirgilPageUpdate"}:
            page = variables["page"]
            stored = {
                "id": variables.get("id", "gid://shopify/Page/1"),
                "handle": page["handle"],
                "title": page["title"],
                "body": page["body"],
                "isPublished": page["isPublished"],
            }
            self.pages[page["handle"]] = stored
            self.mutations.append(operation)
            field = "pageCreate" if operation.endswith("Create") else "pageUpdate"
            return self._reply({"data": {field: {"page": stored, "userErrors": []}}})
        if operation == "VirgilMenus":
            assert variables == {}
            return self._reply({
                "data": {
                    "menus": {
                        "nodes": list(self.menus.values()),
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            })
        if operation in {"VirgilMenuCreate", "VirgilMenuUpdate"}:
            stored = {
                "id": variables.get("id", "gid://shopify/Menu/1"),
                "handle": variables["handle"],
                "title": variables["title"],
                "items": variables["items"],
            }
            self.menus[variables["handle"]] = stored
            self.mutations.append(operation)
            field = "menuCreate" if operation.endswith("Create") else "menuUpdate"
            return self._reply({"data": {field: {"menu": stored, "userErrors": []}}})
        if operation == "VirgilThemes":
            return self._reply({
                "data": {
                    "themes": {
                        "nodes": [
                            {
                                "id": "gid://shopify/OnlineStoreTheme/1",
                                "name": "Dawn",
                                "role": "MAIN",
                                "processing": False,
                                "processingFailed": False,
                                "themeStoreId": 887,
                            }
                        ]
                    }
                }
            })
        if operation == "VirgilThemeSettings":
            return self._reply({
                "data": {
                    "theme": {
                        "id": variables["id"],
                        "files": {
                            "nodes": [
                                {
                                    "filename": "config/settings_data.json",
                                    "body": {"content": self.theme_settings},
                                }
                            ]
                        },
                    }
                }
            })
        if operation == "VirgilThemeSettingsUpsert":
            self.theme_settings = variables["files"][0]["body"]["value"]
            self.mutations.append(operation)
            return self._reply({
                "data": {
                    "themeFilesUpsert": {
                        "upsertedThemeFiles": [
                            {"filename": "config/settings_data.json"}
                        ],
                        "job": None,
                        "userErrors": [],
                    }
                }
            })
        if operation == "VirgilCustomerByEmail":
            nodes = (
                []
                if self.customer_deleted
                else [
                    {
                        "id": "gid://shopify/Customer/1",
                        "defaultEmailAddress": {
                            "emailAddress": self.customer_email,
                            "marketingState": "SUBSCRIBED",
                            "marketingOptInLevel": "CONFIRMED_OPT_IN",
                            "marketingUpdatedAt": "2026-08-02T00:00:00Z",
                        },
                    }
                ]
            )
            return self._reply({"data": {"customers": {"nodes": nodes}}})
        if operation == "VirgilCustomerDelete":
            assert variables["id"] == "gid://shopify/Customer/1"
            self.customer_deleted = True
            self.mutations.append(operation)
            return self._reply({
                "data": {
                    "customerDelete": {
                        "deletedCustomerId": variables["id"],
                        "userErrors": [],
                    }
                }
            })
        raise AssertionError(f"unexpected operation {operation}")

    @staticmethod
    def _reply(document):
        return 200, json.dumps(document, separators=(",", ":")).encode()


@pytest.fixture
def content():
    return build_content({
        "contact_email": "launch@example.test",
        "business_identity_sentence": "Silicon Current is operated by Example Trading.",
        "double_opt_in": True,
        "brand_signoff": True,
        "privacy_signoff": True,
    })


def test_identity_domain_customer_and_explicit_fallback_capabilities():
    fake = FakeShopify()
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)

    identity = client.shop_identity()
    assert identity["id"] == "gid://shopify/Shop/1"
    assert identity["currency"] == "AUD"
    assert client.domain_status("siliconcurrent.com") == {
        "id": "gid://shopify/Domain/1",
        "host": "siliconcurrent.com",
        "url": "https://siliconcurrent.com",
        "ssl_enabled": True,
        "connected": True,
        "primary": True,
    }
    customer = client.customer_by_email("waitlist-test@example.test")
    assert customer == {
        "id": "gid://shopify/Customer/1",
        "email": "waitlist-test@example.test",
        "marketing_state": "SUBSCRIBED",
        "opt_in_level": "CONFIRMED_OPT_IN",
        "marketing_updated_at": "2026-08-02T00:00:00Z",
    }
    assert client.capabilities()["domain_connect"] == {
        "supported": False,
        "fallback": "g_domain_viewer_gate",
    }
    assert client.capabilities()["storefront_password_mutation"]["supported"] is False
    assert client.capabilities()["store_settings_mutation"]["supported"] is False
    assert client.capabilities()["double_opt_in_setting"]["supported"] is False
    assert "waitlist-test@example.test" not in repr(client.__dict__)


def test_client_credentials_exchange_is_pinned_strict_and_secret_safe():
    client_id = "test-client-id"
    client_secret = "test-client-secret"

    def issue_token(request, timeout, limit):
        assert request.full_url == (
            "https://silicon-current.myshopify.com/admin/oauth/access_token"
        )
        assert request.method == "POST"
        assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
        assert timeout > 0
        assert limit == MAX_RESPONSE_BYTES
        assert parse_qs(request.data.decode("ascii"), strict_parsing=True) == {
            "grant_type": ["client_credentials"],
            "client_id": [client_id],
            "client_secret": [client_secret],
        }
        return 200, json.dumps({
            "access_token": TOKEN,
            "scope": ",".join(reversed(SHOPIFY_SCOPES)),
            "expires_in": TOKEN_EXPIRES_IN,
        }).encode()

    client = ShopifyAdminClient.from_client_credentials(
        "silicon-current.myshopify.com",
        client_id,
        client_secret,
        token_transport=issue_token,
        transport=FakeShopify(),
    )

    assert client.shop_identity()["id"] == "gid://shopify/Shop/1"
    rendered = repr(client.__dict__)
    assert TOKEN not in rendered
    assert client_id not in rendered
    assert client_secret not in rendered
    assert "REDACTED_SHOPIFY_SECRET" in rendered


def test_client_credentials_fail_closed_on_expiry_scopes_size_and_exception_text():
    client_secret = "secret-that-must-not-escape"

    def response(document):
        def exchange(request, timeout, limit):
            return 200, json.dumps(document).encode()

        return exchange

    base = {
        "access_token": TOKEN,
        "scope": ",".join(SHOPIFY_SCOPES),
        "expires_in": TOKEN_EXPIRES_IN,
    }
    with pytest.raises(ShopifyResponseError, match="expiry"):
        ShopifyAdminClient.from_client_credentials(
            "silicon-current.myshopify.com",
            "test-client-id",
            client_secret,
            token_transport=response({**base, "expires_in": 86_400}),
        )
    with pytest.raises(ShopifyAuthorizationError, match="scopes"):
        ShopifyAdminClient.from_client_credentials(
            "silicon-current.myshopify.com",
            "test-client-id",
            client_secret,
            token_transport=response({
                **base,
                "scope": ",".join(SHOPIFY_SCOPES[:-1]),
            }),
        )

    def oversized(request, timeout, limit):
        return 200, b"x" * (limit + 1)

    with pytest.raises(ShopifyResponseError, match="too large"):
        ShopifyAdminClient.from_client_credentials(
            "silicon-current.myshopify.com",
            "test-client-id",
            client_secret,
            token_transport=oversized,
        )

    def leaking(request, timeout, limit):
        raise RuntimeError(f"provider echoed {client_secret}")

    with pytest.raises(ShopifyTransportError) as captured:
        ShopifyAdminClient.from_client_credentials(
            "silicon-current.myshopify.com",
            "test-client-id",
            client_secret,
            token_transport=leaking,
        )
    assert client_secret not in str(captured.value)


def test_test_customer_cleanup_is_prefix_bound_and_idempotent():
    fake = FakeShopify()
    fake.customer_email = "waitlist-test+job-1@example.test"
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)

    with pytest.raises(ShopifyConfigurationError, match="waitlist-test"):
        client.delete_test_customer("ordinary-customer@example.test")
    first = client.delete_test_customer("waitlist-test+job-1@example.test")
    second = client.delete_test_customer("waitlist-test+job-1@example.test")

    assert first == {"id": "gid://shopify/Customer/1", "changed": True}
    assert second == {"id": None, "changed": False}
    assert fake.mutations == ["VirgilCustomerDelete"]
    assert "write_customers" in client.capabilities()["scopes"]


def test_identity_rejects_provider_boolean_coercion():
    fake = FakeShopify()
    fake.identity["data"]["shop"]["checkoutApiSupported"] = "false"
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)
    with pytest.raises(ShopifyResponseError, match="boolean"):
        client.shop_identity()


def test_page_and_menu_upserts_are_idempotent(content):
    fake = FakeShopify()
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)
    page = content["pages"]["priority-access"]
    first_page = client.upsert_page(
        handle="priority-access",
        title=page["title"],
        body_html=page["body_html"],
        is_published=page["is_published"],
    )
    second_page = client.upsert_page(
        handle="priority-access",
        title=page["title"],
        body_html=page["body_html"],
        is_published=page["is_published"],
    )
    navigation = content["navigation"]
    first_menu = client.upsert_menu(**navigation)
    second_menu = client.upsert_menu(**navigation)

    assert first_page["changed"] is True and second_page["changed"] is False
    assert first_menu["changed"] is True and second_menu["changed"] is False
    assert fake.mutations == ["VirgilPageCreate", "VirgilMenuCreate"]


def test_menu_accepts_exactly_three_levels_and_rejects_a_fourth():
    fake = FakeShopify()
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)
    items = [
        {
            "title": "One",
            "type": "HTTP",
            "url": "/pages/one",
            "items": [
                {
                    "title": "Two",
                    "type": "HTTP",
                    "url": "/pages/two",
                    "items": [
                        {
                            "title": "Three",
                            "type": "HTTP",
                            "url": "/pages/three",
                        }
                    ],
                }
            ],
        }
    ]

    assert (
        client.upsert_menu(handle="three-levels", title="Three Levels", items=items)[
            "changed"
        ]
        is True
    )
    items[0]["items"][0]["items"][0]["items"] = [
        {"title": "Four", "type": "HTTP", "url": "/pages/four"}
    ]
    with pytest.raises(ShopifyConfigurationError, match="three-level"):
        client.upsert_menu(handle="too-deep", title="Too Deep", items=items)


def test_theme_settings_require_exemption_and_double_apply_is_read_only():
    fake = FakeShopify()
    client = ShopifyAdminClient("silicon-current.myshopify.com", TOKEN, transport=fake)
    with pytest.raises(ShopifyUnsupportedError, match="exemption"):
        client.upsert_theme_settings({"current": {}})

    authorized = ShopifyAdminClient(
        "silicon-current.myshopify.com",
        TOKEN,
        transport=fake,
        theme_file_write_authorized=True,
    )
    first = authorized.upsert_theme_settings({"current": {"sections": {}}})
    second = authorized.upsert_theme_settings({"current": {"sections": {}}})
    with pytest.raises(ShopifyConfigurationError, match="JSON serializable"):
        authorized.upsert_theme_settings(
            {"not_json": {"a", "set"}},
            theme_id="gid://shopify/OnlineStoreTheme/1",
        )

    assert first["changed"] is True and second["changed"] is False
    assert fake.mutations == ["VirgilThemeSettingsUpsert"]
    assert authorized.main_theme()["name"] == "Dawn"


def test_storefront_probe_is_bounded_pinned_and_detects_password_page():
    seen = []

    def storefront(url, timeout, limit):
        seen.append((url, timeout, limit))
        return 200, {}, b'<main class="shopify-section-main-password"></main>'

    client = ShopifyAdminClient(
        "silicon-current.myshopify.com",
        TOKEN,
        transport=FakeShopify(),
        storefront_transport=storefront,
    )
    assert client.storefront_probe() == {
        "status": 200,
        "location": None,
        "password_protected": True,
        "body_bytes": 51,
    }
    assert seen[0][0] == "https://silicon-current.myshopify.com/"
    with pytest.raises(ShopifyConfigurationError):
        client.storefront_probe("//evil.test/")


def test_storefront_probe_rejects_script_and_cross_port_redirects():
    for location in (
        "javascript:alert(1)",
        "https://silicon-current.myshopify.com:444/",
        "//evil.test/",
    ):

        def redirected(url, timeout, limit):
            return 302, {"Location": location}, b""

        client = ShopifyAdminClient(
            "silicon-current.myshopify.com",
            TOKEN,
            transport=FakeShopify(),
            storefront_transport=redirected,
        )
        with pytest.raises(ShopifyResponseError, match="redirect"):
            client.storefront_probe()


def test_host_errors_and_secrets_fail_closed():
    with pytest.raises(ShopifyConfigurationError):
        ShopifyAdminClient("evil.test", TOKEN)
    with pytest.raises(ShopifyConfigurationError):
        ShopifyAdminClient("silicon-current.myshopify.com.evil.test", TOKEN)

    def leaking_transport(request, timeout, limit):
        raise RuntimeError(f"upstream included {TOKEN}")

    client = ShopifyAdminClient(
        "silicon-current.myshopify.com", TOKEN, transport=leaking_transport
    )
    with pytest.raises(ShopifyConfigurationError, match="domain host"):
        client.domain_status("invalid host")
    with pytest.raises(ShopifyTransportError) as captured:
        client.shop_identity()
    assert TOKEN not in str(captured.value)
    assert str(captured.value) == "Shopify VirgilShopIdentity transport failure"
    assert TOKEN not in repr(client)


def test_graphql_errors_are_typed_without_provider_text_or_response_overflow():
    def denied(request, timeout, limit):
        return 200, json.dumps({
            "errors": [
                {
                    "message": f"provider leaked {TOKEN}",
                    "extensions": {"code": "ACCESS_DENIED"},
                }
            ]
        }).encode()

    client = ShopifyAdminClient(
        "silicon-current.myshopify.com", TOKEN, transport=denied
    )
    with pytest.raises(ShopifyAuthorizationError) as captured:
        client.shop_identity()
    assert TOKEN not in str(captured.value)

    def oversized(request, timeout, limit):
        return 200, b"x" * (limit + 1)

    client = ShopifyAdminClient(
        "silicon-current.myshopify.com", TOKEN, transport=oversized
    )
    with pytest.raises(ShopifyResponseError, match="too large"):
        client.shop_identity()


def test_navigation_hard_fails_checkout_and_product_links():
    client = ShopifyAdminClient(
        "silicon-current.myshopify.com", TOKEN, transport=FakeShopify()
    )
    for path in ("/cart", "/checkout", "/products/card", "/collections/all"):
        with pytest.raises(ShopifyConfigurationError, match="no-checkout"):
            client.upsert_menu(
                handle="footer",
                title="Footer",
                items=[{"title": "Forbidden", "type": "HTTP", "url": path}],
            )
