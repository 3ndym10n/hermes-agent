"""Small, fail-closed Shopify Admin GraphQL 2026-07 adapter for commerce V1.

The client pins one ``*.myshopify.com`` host, disables proxies and redirects,
makes every request once, bounds response bodies, and keeps its access token
only on the live client object. Provider response text is never put in errors.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from commerce_content import assert_claim_free

API_VERSION = "2026-07"
MAX_REQUEST_BYTES = 524_288
MAX_RESPONSE_BYTES = 1_048_576
TIMEOUT_SECONDS = 20
TOKEN_EXPIRES_IN = 86_399
SHOPIFY_SCOPES = (
    "read_customers",
    "write_customers",
    "read_online_store_navigation",
    "write_online_store_navigation",
    "read_online_store_pages",
    "write_online_store_pages",
    "read_themes",
    "write_themes",
)

_SHOP = re.compile(r"(?a)^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com$")
_DOMAIN = re.compile(
    r"(?a)^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_HANDLE = re.compile(r"(?a)^[a-z0-9](?:[a-z0-9-]{0,253}[a-z0-9])?$")
_GID = re.compile(r"^gid://shopify/[A-Za-z][A-Za-z0-9]*/[A-Za-z0-9_-]+$")
_EMAIL = re.compile(
    r"(?a)^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+$", re.I
)
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_PATH = re.compile(r"(?a)^/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*$")
_FORBIDDEN_PATHS = ("/cart", "/checkout", "/products", "/collections")

GraphQLTransport = Callable[[urllib.request.Request, float, int], tuple[int, bytes]]
StorefrontTransport = Callable[[str, float, int], tuple[int, Mapping[str, str], bytes]]


class ShopifyError(RuntimeError):
    """Base class for errors safe to show or persist."""


class ShopifyConfigurationError(ShopifyError):
    pass


class ShopifyTransportError(ShopifyError):
    pass


class ShopifyResponseError(ShopifyError):
    pass


class ShopifyAuthenticationError(ShopifyError):
    pass


class ShopifyAuthorizationError(ShopifyError):
    pass


class ShopifyGraphQLError(ShopifyError):
    def __init__(self, operation: str, codes: Sequence[str] = ("GRAPHQL_ERROR",)):
        self.operation = operation
        self.codes = tuple(codes)
        super().__init__(f"Shopify {operation} failed: {','.join(self.codes)}")


class ShopifyUserError(ShopifyGraphQLError):
    pass


class ShopifyUnsupportedError(ShopifyError):
    pass


class ShopifyIdentityMismatch(ShopifyError):
    pass


class _Secret(str):
    def __repr__(self) -> str:
        return "[REDACTED_SHOPIFY_SECRET]"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _shop_domain(value: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.lower()
        or not _SHOP.fullmatch(value)
    ):
        raise ShopifyConfigurationError("shop must be a lowercase myshopify.com host")
    return value


def _credential(value: str, label: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ShopifyConfigurationError(f"Shopify {label} is invalid")
    return value


def _token(value: str) -> str:
    return _credential(value, "access token", maximum=4096)


def _handle(value: str) -> str:
    if not isinstance(value, str) or not _HANDLE.fullmatch(value):
        raise ShopifyConfigurationError("Shopify handle is invalid")
    return value


def _gid(value: object, where: str) -> str:
    if not isinstance(value, str) or not _GID.fullmatch(value):
        raise ShopifyResponseError(f"{where} is not a valid Shopify GID")
    return value


def _mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShopifyResponseError(f"{where} must be an object")
    return value


def _list(value: object, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise ShopifyResponseError(f"{where} must be an array")
    return value


def _string(value: object, where: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ShopifyResponseError(f"{where} must be bounded non-empty text")
    return value


def _boolean(value: object, where: str) -> bool:
    if not isinstance(value, bool):
        raise ShopifyResponseError(f"{where} must be boolean")
    return value


def _error_codes(errors: object) -> tuple[str, ...]:
    codes: list[str] = []
    for raw_error in _list(errors, "GraphQL errors"):
        error = _mapping(raw_error, "GraphQL error")
        extensions = error.get("extensions")
        code = extensions.get("code") if isinstance(extensions, dict) else None
        codes.append(
            code
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
            else "GRAPHQL_ERROR"
        )
    return tuple(codes) or ("GRAPHQL_ERROR",)


def _user_errors(payload: Mapping[str, object], operation: str) -> None:
    raw = payload.get("userErrors", [])
    if not isinstance(raw, list):
        raise ShopifyResponseError(f"{operation}.userErrors must be an array")
    if not raw:
        return
    codes: list[str] = []
    for entry in raw:
        item = _mapping(entry, f"{operation}.userErrors[]")
        code = item.get("code")
        codes.append(
            code
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
            else "USER_ERROR"
        )
    raise ShopifyUserError(operation, codes)


class ShopifyAdminClient:
    """One-shop Admin API client; no credential loading or persistence."""

    def __init__(
        self,
        shop: str,
        access_token: str,
        *,
        timeout: float = TIMEOUT_SECONDS,
        transport: GraphQLTransport | None = None,
        storefront_transport: StorefrontTransport | None = None,
        theme_file_write_authorized: bool = False,
    ):
        self.shop = _shop_domain(shop)
        self._access_token = _Secret(_token(access_token))
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ShopifyConfigurationError("timeout must be positive")
        if not isinstance(theme_file_write_authorized, bool):
            raise ShopifyConfigurationError(
                "theme_file_write_authorized must be boolean"
            )
        self._timeout = float(timeout)
        self._transport = transport
        self._storefront_transport = storefront_transport
        self._theme_file_write_authorized = theme_file_write_authorized
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect
        )
        self._endpoint = f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json"

    @classmethod
    def from_client_credentials(
        cls,
        shop: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = TIMEOUT_SECONDS,
        token_transport: GraphQLTransport | None = None,
        transport: GraphQLTransport | None = None,
        storefront_transport: StorefrontTransport | None = None,
        theme_file_write_authorized: bool = False,
    ) -> ShopifyAdminClient:
        """Exchange owned-app credentials and return a client without returning the token."""
        shop = _shop_domain(shop)
        client_id = _credential(client_id, "client ID", maximum=512)
        client_secret = _credential(client_secret, "client secret", maximum=4096)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ShopifyConfigurationError("timeout must be positive")
        if not isinstance(theme_file_write_authorized, bool):
            raise ShopifyConfigurationError(
                "theme_file_write_authorized must be boolean"
            )
        timeout = float(timeout)
        body = urlencode((
            ("grant_type", "client_credentials"),
            ("client_id", client_id),
            ("client_secret", client_secret),
        )).encode("ascii")
        if len(body) > MAX_REQUEST_BYTES:
            raise ShopifyConfigurationError("Shopify token request is too large")
        endpoint = f"https://{shop}/admin/oauth/access_token"
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        if token_transport is None:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _NoRedirect
            )

            def token_transport(
                request: urllib.request.Request, timeout: float, limit: int
            ) -> tuple[int, bytes]:
                try:
                    with opener.open(request, timeout=timeout) as response:
                        return response.status, response.read(limit + 1)
                except urllib.error.HTTPError as error:
                    return error.code, error.read(limit + 1)

        try:
            status, raw = token_transport(request, timeout, MAX_RESPONSE_BYTES)
        except Exception:
            raise ShopifyTransportError(
                "Shopify token exchange transport failure"
            ) from None
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(raw, bytes)
        ):
            raise ShopifyTransportError(
                "Shopify token exchange transport returned an invalid result"
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ShopifyResponseError("Shopify token response is too large")
        if status in {401, 403}:
            raise ShopifyAuthenticationError(f"Shopify token exchange HTTP {status}")
        if status != 200:
            raise ShopifyTransportError(f"Shopify token exchange HTTP {status}")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            raise ShopifyResponseError(
                "Shopify token exchange returned invalid JSON"
            ) from None
        token_response = _mapping(document, "Shopify token response")
        if set(token_response) != {"access_token", "scope", "expires_in"}:
            raise ShopifyResponseError("Shopify token response has an invalid shape")
        expires_in = token_response["expires_in"]
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in != TOKEN_EXPIRES_IN
        ):
            raise ShopifyResponseError("Shopify token expiry is invalid")
        scope = token_response["scope"]
        if not isinstance(scope, str) or not scope or len(scope) > 4096:
            raise ShopifyResponseError("Shopify token scope is invalid")
        granted = scope.split(",")
        if (
            any(not item or item != item.strip() for item in granted)
            or len(granted) != len(set(granted))
            or set(granted) != set(SHOPIFY_SCOPES)
        ):
            raise ShopifyAuthorizationError(
                "Shopify token exchange granted unexpected scopes"
            )
        try:
            access_token = _token(token_response["access_token"])
        except ShopifyConfigurationError:
            raise ShopifyResponseError(
                "Shopify token response has an invalid token"
            ) from None
        return cls(
            shop,
            access_token,
            timeout=timeout,
            transport=transport,
            storefront_transport=storefront_transport,
            theme_file_write_authorized=theme_file_write_authorized,
        )

    @property
    def endpoint(self) -> str:
        return self._endpoint

    def __repr__(self) -> str:
        return f"ShopifyAdminClient(shop={self.shop!r}, api_version={API_VERSION!r})"

    def capabilities(self) -> dict:
        return {
            "api_version": API_VERSION,
            "scopes": SHOPIFY_SCOPES,
            "protected_customer_data": "approval_required",
            "credential_flow": {
                "type": "client_credentials",
                "expires_in": TOKEN_EXPIRES_IN,
                "refresh": "same_grant",
            },
            "theme_file_write": {
                "supported": self._theme_file_write_authorized,
                "scope": "write_themes",
                "shopify_exemption_required": True,
                "fallback": "shopify_cli_then_g_theme",
            },
            "domain_connect": {
                "supported": False,
                "fallback": "g_domain_viewer_gate",
            },
            "storefront_password_mutation": {
                "supported": False,
                "fallback": "g_publish_click_viewer_gate",
            },
            "store_settings_mutation": {
                "supported": False,
                "fallback": "g_store_viewer_gate",
            },
            "double_opt_in_setting": {
                "supported": False,
                "fallback": "g_store_viewer_gate",
            },
        }

    def _default_transport(
        self, request: urllib.request.Request, timeout: float, limit: int
    ) -> tuple[int, bytes]:
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, response.read(limit + 1)
        except urllib.error.HTTPError as error:
            return error.code, error.read(limit + 1)

    def _graphql(
        self, operation: str, query: str, variables: Mapping[str, object] | None = None
    ) -> dict[str, Any]:
        body = json.dumps(
            {"operationName": operation, "query": query, "variables": variables or {}},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise ShopifyConfigurationError("Shopify GraphQL request is too large")
        request = urllib.request.Request(
            self._endpoint,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": self._access_token,
            },
            method="POST",
        )
        transport = self._transport or self._default_transport
        try:
            status, raw = transport(request, self._timeout, MAX_RESPONSE_BYTES)
        except Exception:
            raise ShopifyTransportError(
                f"Shopify {operation} transport failure"
            ) from None
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not isinstance(raw, bytes)
        ):
            raise ShopifyTransportError(
                f"Shopify {operation} transport returned an invalid result"
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ShopifyResponseError(f"Shopify {operation} response is too large")
        if status in {401, 403}:
            error_type = (
                ShopifyAuthenticationError
                if status == 401
                else ShopifyAuthorizationError
            )
            raise error_type(f"Shopify {operation} HTTP {status}")
        if status != 200:
            raise ShopifyTransportError(f"Shopify {operation} HTTP {status}")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError):
            raise ShopifyResponseError(
                f"Shopify {operation} returned invalid JSON"
            ) from None
        root = _mapping(document, "GraphQL response")
        if root.get("errors"):
            codes = _error_codes(root["errors"])
            error_type = (
                ShopifyAuthorizationError
                if "ACCESS_DENIED" in codes
                else ShopifyGraphQLError
            )
            if error_type is ShopifyAuthorizationError:
                raise error_type(f"Shopify {operation} access denied")
            raise error_type(operation, codes)
        return _mapping(root.get("data"), "GraphQL response.data")

    def shop_identity(self) -> dict:
        data = self._graphql(
            "VirgilShopIdentity",
            """query VirgilShopIdentity { shop { id name myshopifyDomain currencyCode ianaTimezone checkoutApiSupported plan { displayName publicDisplayName partnerDevelopment } primaryDomain { id host url sslEnabled } domains { id host url sslEnabled } } }""",
        )
        shop = _mapping(data.get("shop"), "shop")
        myshopify_domain = _string(shop.get("myshopifyDomain"), "shop.myshopifyDomain")
        if myshopify_domain != self.shop:
            raise ShopifyIdentityMismatch("Shopify token resolved to a different shop")
        plan = _mapping(shop.get("plan"), "shop.plan")
        domains = _list(shop.get("domains"), "shop.domains")
        return {
            "id": _gid(shop.get("id"), "shop.id"),
            "name": _string(shop.get("name"), "shop.name", maximum=255),
            "myshopify_domain": myshopify_domain,
            "currency": _string(
                shop.get("currencyCode"), "shop.currencyCode", maximum=3
            ),
            "timezone": _string(
                shop.get("ianaTimezone"), "shop.ianaTimezone", maximum=255
            ),
            "checkout_api_supported": _boolean(
                shop.get("checkoutApiSupported"), "shop.checkoutApiSupported"
            ),
            "plan": _string(
                plan.get("publicDisplayName") or plan.get("displayName"),
                "shop.plan",
                maximum=128,
            ),
            "partner_development": _boolean(
                plan.get("partnerDevelopment"), "shop.plan.partnerDevelopment"
            ),
            "primary_domain": self._domain(
                _mapping(shop.get("primaryDomain"), "shop.primaryDomain")
            ),
            "domains": tuple(
                self._domain(_mapping(item, "shop.domains[]")) for item in domains
            ),
        }

    @staticmethod
    def _domain(raw: Mapping[str, object]) -> dict:
        host = _string(raw.get("host"), "domain.host", maximum=253).lower()
        url = _string(raw.get("url"), "domain.url", maximum=2048)
        if not _DOMAIN.fullmatch(host):
            raise ShopifyResponseError("domain.host is invalid")
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc != host
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ShopifyResponseError("domain.url does not match domain.host")
        return {
            "id": _gid(raw.get("id"), "domain.id"),
            "host": host,
            "url": url,
            "ssl_enabled": _boolean(raw.get("sslEnabled"), "domain.sslEnabled"),
        }

    def domain_status(self, host: str) -> dict:
        if (
            not isinstance(host, str)
            or host != host.lower()
            or not _DOMAIN.fullmatch(host)
        ):
            raise ShopifyConfigurationError("domain host is invalid")
        identity = self.shop_identity()
        for domain in identity["domains"]:
            if domain["host"] == host:
                return {
                    **domain,
                    "connected": True,
                    "primary": identity["primary_domain"]["host"] == host,
                }
        return {
            "host": host,
            "connected": False,
            "primary": False,
            "ssl_enabled": False,
        }

    def _pages(self, handle: str) -> list[dict]:
        data = self._graphql(
            "VirgilPages",
            """query VirgilPages($query: String!) { pages(first: 2, query: $query) { nodes { id handle title body isPublished } } }""",
            {"query": f"handle:{handle}"},
        )
        connection = _mapping(data.get("pages"), "pages")
        result: list[dict] = []
        for raw in _list(connection.get("nodes"), "pages.nodes"):
            page = _mapping(raw, "pages.nodes[]")
            result.append({
                "id": _gid(page.get("id"), "page.id"),
                "handle": _handle(
                    _string(page.get("handle"), "page.handle", maximum=255)
                ),
                "title": _string(page.get("title"), "page.title", maximum=255),
                "body": page.get("body") if isinstance(page.get("body"), str) else "",
                "is_published": page.get("isPublished") is True,
            })
        return [page for page in result if page["handle"] == handle]

    def upsert_page(
        self,
        *,
        handle: str,
        title: str,
        body_html: str,
        is_published: bool = True,
    ) -> dict:
        handle = _handle(handle)
        if not isinstance(title, str) or not 1 <= len(title) <= 255:
            raise ShopifyConfigurationError("page title is invalid")
        if (
            not isinstance(body_html, str)
            or len(body_html.encode("utf-8")) > MAX_REQUEST_BYTES // 2
        ):
            raise ShopifyConfigurationError("page body is invalid or too large")
        if not isinstance(is_published, bool):
            raise ShopifyConfigurationError("is_published must be boolean")
        assert_claim_free(title)
        assert_claim_free(body_html)
        existing = self._pages(handle)
        if len(existing) > 1:
            raise ShopifyResponseError(
                "multiple Shopify pages use the requested handle"
            )
        page_input = {
            "handle": handle,
            "title": title,
            "body": body_html,
            "isPublished": is_published,
        }
        if existing and all(
            existing[0][key] == value
            for key, value in {
                "handle": handle,
                "title": title,
                "body": body_html,
                "is_published": is_published,
            }.items()
        ):
            return {"id": existing[0]["id"], "handle": handle, "changed": False}
        if existing:
            operation = "VirgilPageUpdate"
            data = self._graphql(
                operation,
                """mutation VirgilPageUpdate($id: ID!, $page: PageUpdateInput!) { pageUpdate(id: $id, page: $page) { page { id handle } userErrors { code field message } } }""",
                {"id": existing[0]["id"], "page": page_input},
            )
            payload = _mapping(data.get("pageUpdate"), "pageUpdate")
        else:
            operation = "VirgilPageCreate"
            data = self._graphql(
                operation,
                """mutation VirgilPageCreate($page: PageCreateInput!) { pageCreate(page: $page) { page { id handle } userErrors { code field message } } }""",
                {"page": page_input},
            )
            payload = _mapping(data.get("pageCreate"), "pageCreate")
        _user_errors(payload, operation)
        page = _mapping(payload.get("page"), f"{operation}.page")
        if _handle(_string(page.get("handle"), "page.handle", maximum=255)) != handle:
            raise ShopifyResponseError(
                "Shopify page mutation returned the wrong handle"
            )
        return {
            "id": _gid(page.get("id"), "page.id"),
            "handle": handle,
            "changed": True,
        }

    @staticmethod
    def _menu_items(
        raw_items: object, *, response: bool = False, depth: int = 1
    ) -> list[dict]:
        items = _list(raw_items, "menu items")
        if depth > 3 and items:
            raise ShopifyConfigurationError(
                "menu nesting exceeds Shopify's three-level limit"
            )
        if len(items) > 50:
            raise ShopifyConfigurationError("menu contains too many items")
        normalized: list[dict] = []
        for raw in items:
            item = _mapping(raw, "menu item")
            title = item.get("title")
            item_type = item.get("type")
            url = item.get("url")
            if not isinstance(title, str) or not 1 <= len(title) <= 255:
                raise ShopifyConfigurationError("menu item title is invalid")
            if item_type not in {"HTTP", "PAGE"}:
                raise ShopifyConfigurationError(
                    "commerce menus allow only HTTP and PAGE links"
                )
            if url is not None:
                if not isinstance(url, str) or len(url) > 2048:
                    raise ShopifyConfigurationError("menu item URL is invalid")
                parts = urlsplit(url)
                path = parts.path.lower()
                if (
                    parts.scheme
                    or parts.netloc
                    or not path.startswith("/")
                    or path.startswith(_FORBIDDEN_PATHS)
                ):
                    raise ShopifyConfigurationError(
                        "menu item URL crosses the no-checkout boundary"
                    )
            resource_id = item.get("resourceId")
            if item_type == "PAGE" and resource_id is None:
                raise ShopifyConfigurationError("PAGE menu items require resourceId")
            nested = ShopifyAdminClient._menu_items(
                item.get("items", []), response=response, depth=depth + 1
            )
            clean = {"title": title, "type": item_type, "items": nested}
            if url is not None:
                clean["url"] = url
            if resource_id is not None:
                clean["resourceId"] = _gid(resource_id, "menu item.resourceId")
            tags = item.get("tags", [])
            if tags:
                if not isinstance(tags, list) or not all(
                    isinstance(tag, str) for tag in tags
                ):
                    raise ShopifyConfigurationError("menu item tags are invalid")
                clean["tags"] = tags
            if response and item.get("id") is not None:
                _gid(item["id"], "menu item.id")
            normalized.append(clean)
        return normalized

    def _menus(self, handle: str) -> list[dict]:
        data = self._graphql(
            "VirgilMenus",
            """query VirgilMenus { menus(first: 250) { pageInfo { hasNextPage } nodes { id handle title items { id title type url resourceId tags items { id title type url resourceId tags items { id title type url resourceId tags } } } } } }""",
        )
        connection = _mapping(data.get("menus"), "menus")
        page_info = _mapping(connection.get("pageInfo"), "menus.pageInfo")
        if _boolean(page_info.get("hasNextPage"), "menus.pageInfo.hasNextPage"):
            raise ShopifyResponseError(
                "Shopify menu list exceeds the bounded upsert search"
            )
        result = []
        for raw in _list(connection.get("nodes"), "menus.nodes"):
            menu = _mapping(raw, "menus.nodes[]")
            clean_handle = _handle(
                _string(menu.get("handle"), "menu.handle", maximum=255)
            )
            if clean_handle == handle:
                result.append({
                    "id": _gid(menu.get("id"), "menu.id"),
                    "handle": clean_handle,
                    "title": _string(menu.get("title"), "menu.title", maximum=255),
                    "items": self._menu_items(menu.get("items"), response=True),
                })
        return result

    def upsert_menu(self, *, handle: str, title: str, items: object) -> dict:
        handle = _handle(handle)
        if not isinstance(title, str) or not 1 <= len(title) <= 255:
            raise ShopifyConfigurationError("menu title is invalid")
        assert_claim_free(title)
        clean_items = self._menu_items(items)
        assert_claim_free(json.dumps(clean_items, sort_keys=True))
        existing = self._menus(handle)
        if len(existing) > 1:
            raise ShopifyResponseError(
                "multiple Shopify menus use the requested handle"
            )
        if (
            existing
            and existing[0]["title"] == title
            and existing[0]["items"] == clean_items
        ):
            return {"id": existing[0]["id"], "handle": handle, "changed": False}
        if existing:
            operation = "VirgilMenuUpdate"
            data = self._graphql(
                operation,
                """mutation VirgilMenuUpdate($id: ID!, $title: String!, $handle: String!, $items: [MenuItemUpdateInput!]!) { menuUpdate(id: $id, title: $title, handle: $handle, items: $items) { menu { id handle } userErrors { code field message } } }""",
                {
                    "id": existing[0]["id"],
                    "title": title,
                    "handle": handle,
                    "items": clean_items,
                },
            )
            payload = _mapping(data.get("menuUpdate"), "menuUpdate")
        else:
            operation = "VirgilMenuCreate"
            data = self._graphql(
                operation,
                """mutation VirgilMenuCreate($title: String!, $handle: String!, $items: [MenuItemCreateInput!]!) { menuCreate(title: $title, handle: $handle, items: $items) { menu { id handle } userErrors { code field message } } }""",
                {"title": title, "handle": handle, "items": clean_items},
            )
            payload = _mapping(data.get("menuCreate"), "menuCreate")
        _user_errors(payload, operation)
        menu = _mapping(payload.get("menu"), f"{operation}.menu")
        if _handle(_string(menu.get("handle"), "menu.handle", maximum=255)) != handle:
            raise ShopifyResponseError(
                "Shopify menu mutation returned the wrong handle"
            )
        return {
            "id": _gid(menu.get("id"), "menu.id"),
            "handle": handle,
            "changed": True,
        }

    def themes(self) -> tuple[dict, ...]:
        data = self._graphql(
            "VirgilThemes",
            """query VirgilThemes { themes(first: 20) { nodes { id name role processing processingFailed themeStoreId } } }""",
        )
        connection = _mapping(data.get("themes"), "themes")
        result = []
        for raw in _list(connection.get("nodes"), "themes.nodes"):
            theme = _mapping(raw, "themes.nodes[]")
            role = _string(theme.get("role"), "theme.role", maximum=32)
            if role not in {
                "ARCHIVED",
                "DEMO",
                "DEVELOPMENT",
                "LOCKED",
                "MAIN",
                "MOBILE",
                "UNPUBLISHED",
            }:
                raise ShopifyResponseError("theme.role is invalid")
            result.append({
                "id": _gid(theme.get("id"), "theme.id"),
                "name": _string(theme.get("name"), "theme.name", maximum=255),
                "role": role,
                "processing": _boolean(theme.get("processing"), "theme.processing"),
                "processing_failed": _boolean(
                    theme.get("processingFailed"), "theme.processingFailed"
                ),
                "theme_store_id": theme.get("themeStoreId")
                if isinstance(theme.get("themeStoreId"), int)
                else None,
            })
        return tuple(result)

    def main_theme(self) -> dict:
        themes = [theme for theme in self.themes() if theme["role"] == "MAIN"]
        if len(themes) != 1:
            raise ShopifyResponseError("Shopify did not return exactly one main theme")
        return themes[0]

    def _theme_settings_text(self, theme_id: str) -> str:
        data = self._graphql(
            "VirgilThemeSettings",
            """query VirgilThemeSettings($id: ID!) { theme(id: $id) { id files(filenames: [\"config/settings_data.json\"], first: 1) { nodes { filename body { ... on OnlineStoreThemeFileBodyText { content } } } } } }""",
            {"id": theme_id},
        )
        theme = _mapping(data.get("theme"), "theme")
        if _gid(theme.get("id"), "theme.id") != theme_id:
            raise ShopifyResponseError("Shopify returned the wrong theme")
        files = _mapping(theme.get("files"), "theme.files")
        nodes = _list(files.get("nodes"), "theme.files.nodes")
        if len(nodes) != 1:
            raise ShopifyResponseError("theme settings file is missing or ambiguous")
        file = _mapping(nodes[0], "theme settings file")
        if file.get("filename") != "config/settings_data.json":
            raise ShopifyResponseError("Shopify returned the wrong theme settings file")
        body = _mapping(file.get("body"), "theme settings body")
        return _string(
            body.get("content"), "theme settings content", maximum=MAX_REQUEST_BYTES
        )

    def upsert_theme_settings(
        self, settings: Mapping[str, object], *, theme_id: str | None = None
    ) -> dict:
        if not self._theme_file_write_authorized:
            raise ShopifyUnsupportedError(
                "theme settings require write_themes plus Shopify's theme-file exemption; use Shopify CLI or G-theme"
            )
        if not isinstance(settings, Mapping):
            raise ShopifyConfigurationError("theme settings must be an object")
        theme_id = (
            _gid(theme_id, "theme_id")
            if theme_id is not None
            else self.main_theme()["id"]
        )
        try:
            content = json.dumps(
                settings, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            raise ShopifyConfigurationError(
                "theme settings must be JSON serializable"
            ) from None
        assert_claim_free(content)
        if self._theme_settings_text(theme_id) == content:
            return {
                "theme_id": theme_id,
                "filename": "config/settings_data.json",
                "changed": False,
            }
        operation = "VirgilThemeSettingsUpsert"
        data = self._graphql(
            operation,
            """mutation VirgilThemeSettingsUpsert($themeId: ID!, $files: [OnlineStoreThemeFilesUpsertFileInput!]!) { themeFilesUpsert(themeId: $themeId, files: $files) { upsertedThemeFiles { filename } job { id } userErrors { field message } } }""",
            {
                "themeId": theme_id,
                "files": [
                    {
                        "filename": "config/settings_data.json",
                        "body": {"type": "TEXT", "value": content},
                    }
                ],
            },
        )
        payload = _mapping(data.get("themeFilesUpsert"), "themeFilesUpsert")
        _user_errors(payload, operation)
        files = _list(
            payload.get("upsertedThemeFiles"), "themeFilesUpsert.upsertedThemeFiles"
        )
        if (
            not files
            or _mapping(files[0], "upsertedThemeFiles[0]").get("filename")
            != "config/settings_data.json"
        ):
            raise ShopifyResponseError(
                "Shopify did not confirm the theme settings write"
            )
        job = payload.get("job")
        return {
            "theme_id": theme_id,
            "filename": "config/settings_data.json",
            "changed": True,
            "job_id": _gid(job.get("id"), "job.id")
            if isinstance(job, dict) and job.get("id")
            else None,
        }

    def page_by_handle(self, handle: str) -> dict | None:
        """Read one published page back, for gate verification."""
        pages = self._pages(_handle(handle))
        if len(pages) > 1:
            raise ShopifyResponseError(
                "multiple Shopify pages use the requested handle"
            )
        return pages[0] if pages else None

    def menu_by_handle(self, handle: str) -> dict | None:
        """Read one navigation menu back, for gate verification."""
        menus = self._menus(_handle(handle))
        return menus[0] if menus else None

    def theme_settings_text(self, theme_id: str | None = None) -> str:
        """Read the live theme's settings JSON, for gate verification."""
        resolved = (
            _gid(theme_id, "theme_id")
            if theme_id is not None
            else self.main_theme()["id"]
        )
        return self._theme_settings_text(resolved)

    def commerce_surface(self) -> dict:
        """Read the sellable-surface facts behind the checkout-absent check.

        `paymentSettings` hangs off `shop`, not the query root. It is reported
        verbatim and **not** interpreted: `supportedDigitalWallets` is the list
        of wallets the shop *could* support, which is not evidence that a
        payment provider is configured, and Shopify exposes no documented field
        that is. Checkout absence is therefore proven from customer-facing
        facts in `commerce_verify` -- zero products, no buy/price controls, no
        product route, no reachable cart or checkout.
        """
        data = self._graphql(
            "VirgilCommerceSurface",
            """query VirgilCommerceSurface { productsCount { count } shop { id paymentSettings { supportedDigitalWallets } } }""",
            {},
        )
        products = _mapping(data.get("productsCount"), "productsCount")
        count = products.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ShopifyResponseError("Shopify returned an invalid product count")
        shop = _mapping(data.get("shop"), "shop")
        settings = _mapping(shop.get("paymentSettings"), "shop.paymentSettings")
        wallets = _list(
            settings.get("supportedDigitalWallets"),
            "shop.paymentSettings.supportedDigitalWallets",
        )
        return {
            "products_count": count,
            "supported_digital_wallets": tuple(
                _string(
                    wallet, "shop.paymentSettings.supportedDigitalWallets[]", maximum=64
                )
                for wallet in wallets
            ),
        }

    def customer_by_email(self, email: str) -> dict | None:
        if (
            not isinstance(email, str)
            or len(email) > 254
            or not _EMAIL.fullmatch(email)
        ):
            raise ShopifyConfigurationError("customer email is invalid")
        query_value = email.replace("\\", "\\\\").replace('"', '\\"')
        data = self._graphql(
            "VirgilCustomerByEmail",
            """query VirgilCustomerByEmail($query: String!) { customers(first: 2, query: $query) { nodes { id defaultEmailAddress { emailAddress marketingState marketingOptInLevel marketingUpdatedAt } } } }""",
            {"query": f'email:"{query_value}"'},
        )
        connection = _mapping(data.get("customers"), "customers")
        matches = []
        for raw in _list(connection.get("nodes"), "customers.nodes"):
            customer = _mapping(raw, "customers.nodes[]")
            address = _mapping(
                customer.get("defaultEmailAddress"), "customer.defaultEmailAddress"
            )
            returned_email = _string(
                address.get("emailAddress"), "customer.email", maximum=254
            )
            if returned_email.casefold() == email.casefold():
                matches.append({
                    "id": _gid(customer.get("id"), "customer.id"),
                    "email": returned_email,
                    "marketing_state": _string(
                        address.get("marketingState"),
                        "customer.marketingState",
                        maximum=32,
                    ),
                    "opt_in_level": address.get("marketingOptInLevel")
                    if isinstance(address.get("marketingOptInLevel"), str)
                    else None,
                    "marketing_updated_at": address.get("marketingUpdatedAt")
                    if isinstance(address.get("marketingUpdatedAt"), str)
                    else None,
                })
        if len(matches) > 1:
            raise ShopifyResponseError(
                "multiple customers matched the exact test address"
            )
        return matches[0] if matches else None

    def delete_test_customer(self, email: str) -> dict:
        """Delete only a synthetic waitlist-test+ customer, idempotently."""
        if (
            not isinstance(email, str)
            or len(email) > 254
            or not _EMAIL.fullmatch(email)
            or not email.partition("@")[0].casefold().startswith("waitlist-test+")
            or email.partition("@")[0].casefold() == "waitlist-test+"
        ):
            raise ShopifyConfigurationError(
                "test customer email must use the waitlist-test+ prefix"
            )
        customer = self.customer_by_email(email)
        if customer is None:
            return {"id": None, "changed": False}
        customer_id = customer["id"]
        operation = "VirgilCustomerDelete"
        data = self._graphql(
            operation,
            """mutation VirgilCustomerDelete($id: ID!) { customerDelete(input: { id: $id }) { deletedCustomerId userErrors { field message } } }""",
            {"id": customer_id},
        )
        payload = _mapping(data.get("customerDelete"), "customerDelete")
        _user_errors(payload, operation)
        deleted_id = _gid(
            payload.get("deletedCustomerId"), "customerDelete.deletedCustomerId"
        )
        if deleted_id != customer_id:
            raise ShopifyResponseError(
                "Shopify customer deletion returned the wrong customer"
            )
        return {"id": deleted_id, "changed": True}

    def _default_storefront_transport(
        self, url: str, timeout: float, limit: int
    ) -> tuple[int, Mapping[str, str], bytes]:
        request = urllib.request.Request(
            url, headers={"Accept": "text/html"}, method="GET"
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return response.status, dict(response.headers), response.read(limit + 1)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read(limit + 1)

    def storefront_probe(self, path: str = "/") -> dict:
        if (
            not isinstance(path, str)
            or len(path) > 2048
            or not _SAFE_PATH.fullmatch(path)
            or ".." in path
            or path.startswith("//")
        ):
            raise ShopifyConfigurationError("storefront path is invalid")
        url = f"https://{self.shop}{path}"
        transport = self._storefront_transport or self._default_storefront_transport
        try:
            status, headers, body = transport(url, self._timeout, MAX_RESPONSE_BYTES)
        except Exception:
            raise ShopifyTransportError(
                "Shopify storefront probe transport failure"
            ) from None
        if (
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
            or not isinstance(headers, Mapping)
            or not isinstance(body, bytes)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise ShopifyTransportError(
                "Shopify storefront transport returned an invalid result"
            )
        if len(body) > MAX_RESPONSE_BYTES:
            raise ShopifyResponseError("Shopify storefront response is too large")
        location = headers.get("Location") or headers.get("location")
        if location is not None:
            if (
                not isinstance(location, str)
                or len(location) > 2048
                or "\\" in location
            ):
                raise ShopifyResponseError("Shopify storefront redirect is invalid")
            parts = urlsplit(location)
            if (
                parts.netloc and (parts.scheme != "https" or parts.netloc != self.shop)
            ) or (
                not parts.netloc
                and (
                    parts.scheme
                    or not location.startswith("/")
                    or location.startswith("//")
                )
            ):
                raise ShopifyResponseError(
                    "Shopify storefront redirected off the pinned shop"
                )
        lowered = body.lower()
        password_protected = any(
            marker in lowered
            for marker in (
                b"shopify-section-main-password",
                b"password-page",
                b'action="/password"',
            )
        )
        return {
            "status": status,
            "location": location,
            "password_protected": password_protected,
            "body_bytes": len(body),
        }
