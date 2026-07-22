"""Deterministic semantic checkout discovery for the purchase executor.

Only browser metadata is inspected: origins, forms, labels, roles, standard
payment attributes, and visibility/editability state.  Input values and HTML
are never read.  Discovery is deliberately strict: every required field and
the submit control must have exactly one high-confidence match.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


FIELD_NAMES = ("card_number", "card_expiry", "card_cvv", "card_name")
MIN_CONFIDENCE = 90
MAX_FRAMES = 16
MAX_FRAME_DEPTH = 3
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


# Executed once per frame.  It returns metadata only: never HTML, cookies,
# storage, input values, or account/session state.
DISCOVERY_JS = r"""(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const visible = el => {
    const style = getComputedStyle(el), box = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || 1) !== 0 && box.width > 0 && box.height > 0;
  };
  const path = el => {
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.documentElement) {
      let part = el.localName;
      if (el.id && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {
        parts.unshift(part + '#' + CSS.escape(el.id));
        break;
      }
      const siblings = [...el.parentElement.children].filter(x => x.localName === el.localName);
      part += ':nth-of-type(' + (siblings.indexOf(el) + 1) + ')';
      parts.unshift(part);
      el = el.parentElement;
    }
    return parts.join(' > ');
  };
  const name = el => {
    const aria = clean(el.getAttribute('aria-label'));
    if (aria) return aria;
    const labelled = clean((el.getAttribute('aria-labelledby') || '').split(/\s+/)
      .map(id => document.getElementById(id)?.textContent || '').join(' '));
    if (labelled) return labelled;
    const labels = clean(el.labels ? [...el.labels].map(x => x.textContent || '').join(' ') : '');
    const buttonText = el.localName === 'button' ? clean(el.textContent) : '';
    return labels || buttonText || clean(el.getAttribute('placeholder')) || clean(el.getAttribute('title'));
  };
  const role = el => clean(el.getAttribute('role')) ||
    (el.localName === 'button' || ['submit', 'button'].includes(el.type) ? 'button' :
      ['text', 'tel', 'email', 'password', 'search', 'url', ''].includes(el.type) ? 'textbox' :
      el.type === 'number' ? 'spinbutton' : '');
  const form = el => {
    const f = el.form;
    const context = f ? [f.getAttribute('aria-label'), f.id, f.getAttribute('name')].join(' ') : '';
    return {
      form_key: f ? path(f) : '',
      form_action: f ? f.action : '',
      form_context: /payment|checkout|order/i.test(context),
    };
  };
  const metadata = el => ({
    selector: path(el),
    role: role(el),
    type: clean(el.getAttribute('type') || (el.localName === 'button' ? 'button' : 'text')).toLowerCase(),
    name: clean(el.getAttribute('name')),
    id: clean(el.id),
    autocomplete: clean(el.getAttribute('autocomplete')).toLowerCase(),
    accessible_name: name(el),
    visible: visible(el),
    enabled: !el.disabled,
    readonly: !!el.readOnly,
    ...form(el),
  });
  const fields = [...document.querySelectorAll('input')].map(metadata);
  const submits = [...new Set(document.querySelectorAll(
    'button, input[type="submit"], input[type="button"], [role="button"]'
  ))].map(metadata);
  const frames = [...document.querySelectorAll('iframe')].map(el => ({
    selector: path(el),
    src: el.src,
    hint: clean([el.title, el.name, el.id, el.getAttribute('aria-label')].join(' ')),
    visible: visible(el),
  }));
  const text = document.body ? document.body.innerText : '';
  const challenge = /captcha|are you a robot|one[- ]?time\s+(?:password|code)|verification code|multi[- ]?factor|\bmfa\b/i.test(text);
  const uncertain = /3[- ]?d[- ]?secure|\b3ds\b|verify your card|authentication required|bank authentication/i.test(text);
  return JSON.stringify({url: location.href, fields, submits, frames, challenge, uncertain});
})()"""


_LABELS = {
    "card_number": re.compile(r"^(?:credit |debit )?card (?:number|no\.?|#)$", re.I),
    "card_expiry": re.compile(r"^(?:card )?(?:expiry|expiration)(?: date)?$|^exp(?:iry)? date$", re.I),
    "card_cvv": re.compile(r"^(?:card )?(?:cvc|cvv|csc|security code|verification code)$", re.I),
    "card_name": re.compile(r"^(?:name on card|card ?holder(?: name)?)$", re.I),
}
_AUTOCOMPLETE = {
    "card_number": "cc-number",
    "card_expiry": "cc-exp",
    "card_cvv": "cc-csc",
    "card_name": "cc-name",
}
_IDENTIFIERS = {
    "card_number": {"ccnumber", "cardnumber", "creditcardnumber", "pan"},
    "card_expiry": {"ccexp", "ccexpiry", "cardexpiry", "cardexpiration", "expirationdate"},
    "card_cvv": {"cccsc", "cccvc", "cvv", "cvc", "csc", "securitycode"},
    "card_name": {"ccname", "cardname", "cardholder", "cardholdername", "nameoncard"},
}
_SUBMIT_NAMES = re.compile(
    r"^(?:pay(?: now)?|place order|complete (?:purchase|order)|submit payment|"
    r"confirm (?:purchase|order)|buy now)$",
    re.I,
)
_PAYMENT_FRAME = re.compile(r"payment|card|checkout|secure|stripe|braintree|adyen|square", re.I)
_CAPTCHA_FRAME = re.compile(r"captcha|recaptcha|hcaptcha|turnstile", re.I)
_OTP_NAME = re.compile(r"^(?:code|otp|one[- ]?time (?:password|code)|authentication code)$", re.I)


def payment_frame_hint(value: object) -> bool:
    return bool(_PAYMENT_FRAME.search(str(value or "")))


def metadata_challenge(raw: dict) -> bool:
    if raw.get("challenge"):
        return True
    for field in raw.get("fields") or []:
        if not field.get("visible") or not field.get("enabled"):
            continue
        autocomplete = str(field.get("autocomplete") or "").lower().split()
        name = str(field.get("accessible_name") or "").strip()
        if "one-time-code" in autocomplete or _OTP_NAME.fullmatch(name):
            return True
    return any(
        frame.get("visible")
        and _CAPTCHA_FRAME.search(
            f"{frame.get('src') or ''} {frame.get('hint') or ''}"
        )
        for frame in raw.get("frames") or []
    )


def origin(url: str) -> str:
    """Return a normalized exact origin, or an empty string for an unsafe URL."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            return ""
        host = parts.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        default = (parts.scheme == "http" and parts.port == 80) or (
            parts.scheme == "https" and parts.port == 443
        )
        port = "" if parts.port is None or default else f":{parts.port}"
        return f"{parts.scheme}://{host}{port}"
    except ValueError:
        return ""



def _identifier(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


@dataclass(frozen=True)
class Match:
    field: str
    frame_path: tuple[str, ...]
    frame_origin: str
    form_key: str
    form_action_origin: str
    locator_kind: str
    locator_value: str
    role: str
    type: str
    name: str
    id: str
    autocomplete: str
    accessible_name: str
    confidence: int

    def command(self, action: str, value: str = "") -> list[str]:
        if self.locator_kind == "label":
            return ["find", "label", self.locator_value, action, value, "--exact"] if value else [
                "find", "label", self.locator_value, action, "--exact"
            ]
        if self.locator_kind == "role":
            command = ["find", "role", self.role, action]
            if value:
                command.append(value)
            return command + ["--name", self.locator_value, "--exact"]
        return [action, self.locator_value] + ([value] if value else [])

    def active_expression(self) -> str:
        """Boolean-only proof that the focused ref is this semantic control."""
        if self.locator_kind == "css":
            test = f"e.matches({json.dumps(self.locator_value)})"
        else:
            test = (
                "name(e) === " + json.dumps(self.locator_value)
                + (" && role(e) === " + json.dumps(self.role) if self.locator_kind == "role" else "")
            )
        return (
            "(() => { const e = document.activeElement;"
            " const clean = v => String(v || '').replace(/\s+/g, ' ').trim();"
            " const name = e => clean(e.getAttribute('aria-label')) ||"
            " clean((e.getAttribute('aria-labelledby') || '').split(/\s+/)"
            " .map(id => document.getElementById(id)?.textContent || '').join(' ')) ||"
            " clean(e.labels ? [...e.labels].map(x => x.textContent || '').join(' ') : '') ||"
            " (e.localName === 'button' ? clean(e.textContent) : '') ||"
            " clean(e.getAttribute('placeholder')) || clean(e.getAttribute('title'));"
            " const role = e => clean(e.getAttribute('role')) ||"
            " (e.localName === 'button' || ['submit','button'].includes(e.type) ? 'button' :"
            " ['text','tel','email','password','search','url',''].includes(e.type) ? 'textbox' :"
            " e.type === 'number' ? 'spinbutton' : '');"
            f" return !!e && {test}; }})()"
        )

    def audit(self) -> dict:
        autocomplete = _AUTOCOMPLETE.get(self.field, "")
        has_autocomplete = autocomplete in self.autocomplete.lower().split()
        if has_autocomplete:
            basis = "autocomplete"
        elif self.locator_kind == "role":
            basis = "accessible_role"
        elif self.locator_kind == "label" and self.confidence == 95:
            basis = "accessible_label"
        elif self.locator_kind == "label":
            basis = "adapter_hint"
        else:
            basis = "standard_identifier"
        safe_role = self.role.lower() if self.role.lower() in {"textbox", "spinbutton", "button"} else "other"
        safe_type = self.type.lower() if self.type.lower() in {
            "text", "tel", "number", "password", "submit", "button"
        } else "other"
        return {
            "field": self.field,
            "frame_origin": self.frame_origin,
            "role": safe_role,
            "type": safe_type,
            "name_attribute_present": bool(self.name),
            "id_attribute_present": bool(self.id),
            "autocomplete": autocomplete if has_autocomplete else "",
            "match_basis": basis,
            "confidence": self.confidence,
            "form_action_origin": self.form_action_origin,
        }


@dataclass(frozen=True)
class DiscoveryPlan:
    page_origin: str
    frame_origins: tuple[str, ...]
    fields: tuple[Match, ...]
    submit: Match

    @property
    def fingerprint(self) -> str:
        payload = [
            self.page_origin,
            self.frame_origins,
            [match.__dict__ for match in self.fields],
            self.submit.__dict__,
        ]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def audit(self) -> dict:
        return {
            "page_origin": self.page_origin,
            "frame_origins": list(self.frame_origins),
            "fields": [match.audit() for match in self.fields],
            "submit": self.submit.audit(),
            "fingerprint": self.fingerprint,
        }


class DiscoveryError(RuntimeError):
    def __init__(self, category: str, reason: str, audit: dict | None = None):
        super().__init__(reason)
        self.category = category
        self.reason = reason
        self.audit = audit or {}


def _score_field(raw: dict, field: str, hints: dict[str, tuple[str, ...]]) -> tuple[int, str, str]:
    if not raw.get("visible") or not raw.get("enabled") or raw.get("readonly"):
        return 0, "", ""
    field_type = str(raw.get("type") or "text").lower()
    if field_type in {"hidden", "submit", "button", "checkbox", "radio", "file"}:
        return 0, "", ""
    autocomplete = str(raw.get("autocomplete") or "").lower().split()
    if _AUTOCOMPLETE[field] in autocomplete:
        return 100, "css", f'[autocomplete~="{_AUTOCOMPLETE[field]}"]'
    label = str(raw.get("accessible_name") or "").strip()
    if label and _LABELS[field].fullmatch(label):
        return 95, "label", label
    if any(_identifier(raw.get(key)) in _IDENTIFIERS[field] for key in ("name", "id")):
        return 90, "css", str(raw.get("selector") or "")
    normalized = _identifier(label)
    if normalized and normalized in {_identifier(item) for item in hints.get(field, ())}:
        return 90, "label", label
    return 0, "", ""


def _allowed_origin(candidate: str, allowed: set[str], *, fake_e2e: bool) -> bool:
    if candidate not in allowed:
        return False
    parts = urlsplit(candidate)
    return (parts.hostname or "").lower() in LOOPBACK_HOSTS if fake_e2e else parts.scheme == "https"


def _match(raw: dict, field: str, frame_path: tuple[str, ...], frame_origin: str,
           score: int, locator_kind: str, locator_value: str) -> Match:
    return Match(
        field=field,
        frame_path=frame_path,
        frame_origin=frame_origin,
        form_key=str(raw.get("form_key") or ""),
        form_action_origin=origin(str(raw.get("form_action") or "")),
        locator_kind=locator_kind,
        locator_value=locator_value,
        role=str(raw.get("role") or ""),
        type=str(raw.get("type") or ""),
        name=str(raw.get("name") or ""),
        id=str(raw.get("id") or ""),
        autocomplete=str(raw.get("autocomplete") or ""),
        accessible_name=str(raw.get("accessible_name") or ""),
        confidence=score,
    )


def discover_checkout(inspect_context, *, canonical_domain: str,
                      processor_origins: tuple[str, ...] = (),
                      field_hints: dict[str, tuple[str, ...]] | None = None,
                      submit_hints: tuple[str, ...] = (), fake_e2e: bool = False) -> DiscoveryPlan:
    """Discover one stable, unambiguous payment plan across allowed frames."""
    field_hints = field_hints or {}
    expected_main = "" if fake_e2e else f"https://{canonical_domain.strip().lower()}"
    processors = tuple(origin(item) for item in processor_origins)
    if any(not normalized or "*" in raw or raw != normalized for raw, normalized in zip(processor_origins, processors)):
        raise ValueError("processor origins must be normalized exact origins without wildcards")
    if not fake_e2e and any(urlsplit(item).scheme != "https" for item in processors):
        raise ValueError("production processor origins must use HTTPS")

    queue: list[tuple[str, ...]] = [()]
    contexts: list[tuple[tuple[str, ...], str, dict]] = []
    allowed: set[str] = set(processors)
    main_origin = ""
    while queue:
        frame_path = queue.pop(0)
        if len(contexts) >= MAX_FRAMES:
            raise DiscoveryError("checkout_not_ready", "too_many_frames")
        result = inspect_context(frame_path)
        if not result.get("success") or not isinstance(result.get("result"), dict):
            raise DiscoveryError("checkout_not_ready", "frame_inaccessible")
        raw = result["result"]
        frame_origin = origin(str(raw.get("url") or ""))
        if not frame_path:
            main_origin = frame_origin
            if not main_origin or (expected_main and main_origin != expected_main):
                raise DiscoveryError("wrong_origin", "page_origin_rejected")
            if fake_e2e and (urlsplit(main_origin).hostname or "").lower() not in LOOPBACK_HOSTS:
                raise DiscoveryError("wrong_origin", "page_origin_rejected")
            allowed.add(main_origin)
        elif not _allowed_origin(frame_origin, allowed, fake_e2e=fake_e2e):
            raise DiscoveryError("wrong_origin", "frame_origin_rejected")
        contexts.append((frame_path, frame_origin, raw))

        if len(frame_path) >= MAX_FRAME_DEPTH:
            continue
        for frame in raw.get("frames") or []:
            if not frame.get("visible"):
                continue
            declared = origin(str(frame.get("src") or ""))
            if not _allowed_origin(declared, allowed, fake_e2e=fake_e2e):
                if payment_frame_hint(
                    f"{frame.get('src') or ''} {frame.get('hint') or ''}"
                ):
                    raise DiscoveryError("wrong_origin", "payment_frame_origin_rejected")
                continue
            selector = str(frame.get("selector") or "")
            if selector:
                queue.append(frame_path + (selector,))

    audit_base = {"page_origin": main_origin, "frame_origins": [item[1] for item in contexts[1:]]}
    if any(metadata_challenge(raw) for _, _, raw in contexts):
        raise DiscoveryError("human_challenge_required", "hosted_challenge_detected", audit_base)
    selected: list[Match] = []
    for field in FIELD_NAMES:
        candidates = []
        for frame_path, frame_origin, raw in contexts:
            for element in raw.get("fields") or []:
                score, kind, value = _score_field(element, field, field_hints)
                action_origin = origin(str(element.get("form_action") or ""))
                if score >= MIN_CONFIDENCE and value and _allowed_origin(
                    action_origin, allowed, fake_e2e=fake_e2e
                ):
                    candidates.append(_match(element, field, frame_path, frame_origin, score, kind, value))
        if len(candidates) != 1:
            raise DiscoveryError(
                "checkout_not_ready", f"{field}_{'missing' if not candidates else 'ambiguous'}", audit_base
            )
        selected.append(candidates[0])
    identities = {(item.frame_path, item.locator_kind, item.locator_value) for item in selected}
    if len(identities) != len(FIELD_NAMES):
        raise DiscoveryError("checkout_not_ready", "payment_fields_overlap", audit_base)

    submit_candidates = []
    submit_names = {_identifier(item) for item in submit_hints}
    selected_forms = {(item.frame_path, item.form_key) for item in selected if item.form_key}
    for frame_path, frame_origin, raw in contexts:
        for element in raw.get("submits") or []:
            name = str(element.get("accessible_name") or "").strip()
            action_origin = origin(str(element.get("form_action") or ""))
            associated = (frame_path, str(element.get("form_key") or "")) in selected_forms
            named = bool(_SUBMIT_NAMES.fullmatch(name)) or _identifier(name) in submit_names
            contextual = associated or bool(element.get("form_context"))
            if (element.get("visible") and element.get("enabled") and name and named and contextual
                    and _allowed_origin(action_origin, allowed, fake_e2e=fake_e2e)):
                submit_candidates.append(_match(
                    element, "submit", frame_path, frame_origin,
                    100 if associated else 95, "role", name,
                ))
    if len(submit_candidates) != 1:
        raise DiscoveryError(
            "checkout_not_ready",
            "submit_missing" if not submit_candidates else "submit_ambiguous",
            audit_base,
        )

    return DiscoveryPlan(
        page_origin=main_origin,
        frame_origins=tuple(item[1] for item in contexts[1:]),
        fields=tuple(selected),
        submit=submit_candidates[0],
    )
