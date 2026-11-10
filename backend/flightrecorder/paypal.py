"""A small PayPal REST client: OAuth, Orders v2 and the Disputes API.

Only the standard library is used. HTTP goes through a ``Transport`` so
tests can run against a fake and the real sandbox is only touched on
purpose. Credentials never appear in ``repr`` or in error messages.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# (status, parsed JSON body or {}); raised errors are PayPalError only.
Transport = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, dict]]

# Minor-unit exponent per currency. Anything not listed is refused rather
# than guessed, because a wrong exponent moves the wrong amount of money.
_EXPONENT = {"USD": 2, "EUR": 2, "GBP": 2, "CAD": 2, "AUD": 2, "INR": 2, "JPY": 0}


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, debug_id: str | None = None,
                 *, issues: tuple[str, ...] = ()):
        super().__init__(message)
        self.status = status
        self.debug_id = debug_id
        self.issues = issues


@dataclass(frozen=True)
class PayPalConfig:
    client_id: str
    client_secret: str = field(repr=False)
    env: str = "sandbox"

    def __repr__(self) -> str:
        return f"PayPalConfig(env={self.env!r}, client_id=<hidden>, client_secret=<hidden>)"

    @classmethod
    def from_env(cls, dotenv_path: str | None = ".env") -> "PayPalConfig":
        values = dict(os.environ)
        if dotenv_path and os.path.exists(dotenv_path):
            with open(dotenv_path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        values.setdefault(key.strip(), value.strip())
        env = values.get("PAYPAL_ENV", "sandbox")
        if env != "sandbox":
            raise PayPalError("Only the PayPal sandbox is supported.")
        client_id = values.get("PAYPAL_CLIENT_ID", "")
        secret = values.get("PAYPAL_CLIENT_SECRET", "")
        if not client_id or not secret:
            raise PayPalError("PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set.")
        return cls(client_id, secret, env)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Authorization belongs to the original sandbox endpoint only.
        return None


def urllib_transport(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, dict]:
    if not url.startswith(SANDBOX_URL + "/"):
        raise PayPalError("Only the PayPal sandbox is supported.")
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.getcode()
            if 300 <= status < 400:
                return status, {}
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            declared = response.headers.get("Content-Length")
            if response.headers.get("Transfer-Encoding"):
                declared = None  # Chunk framing is checked by the HTTP reader.
    except (OSError, http.client.HTTPException) as error:
        raise PayPalError(f"Network error talking to PayPal: {type(error).__name__}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise PayPalError("PayPal response exceeded the size limit.")
    if method != "HEAD" and status not in (204, 205) and declared is not None:
        declared = declared.strip()
        if not declared.isascii() or not declared.isdecimal() or (declared.lstrip("0") or "0") != str(len(raw)):
            raise PayPalError("PayPal response did not match its declared length.")
    try:
        data = json.loads(raw) if raw else {}
        return status, data if isinstance(data, dict) else {}
    except (ValueError, RecursionError):
        return status, {}


def _resource_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        raise PayPalError("Invalid PayPal resource ID.")
    return value


def digest_boundary(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()[:24]


def format_amount(minor: int, currency: str) -> str:
    """Integer minor units to PayPal's decimal string, with no floats."""
    if currency not in _EXPONENT:
        raise PayPalError(f"Unsupported currency {currency!r}.")
    if isinstance(minor, bool) or not isinstance(minor, int) or minor < 0:
        raise PayPalError("Amount must be a non-negative integer of minor units.")
    exponent = _EXPONENT[currency]
    if exponent == 0:
        return str(minor)
    whole, frac = divmod(minor, 10**exponent)
    return f"{whole}.{frac:0{exponent}d}"


def parse_amount(value: str, currency: str) -> int:
    """PayPal's decimal string back to integer minor units."""
    if currency not in _EXPONENT:
        raise PayPalError(f"Unsupported currency {currency!r}.")
    exponent = _EXPONENT[currency]
    if not isinstance(value, str) or not value.isascii():
        raise PayPalError("Unreadable amount in PayPal response.")
    whole, dot, frac = value.partition(".")
    if not whole.isdigit() or (dot and not frac.isdigit()) or len(frac) > exponent:
        raise PayPalError("Unreadable amount in PayPal response.")
    return int(whole) * 10**exponent + int(frac.ljust(exponent, "0") or 0)


class PayPalClient:
    def __init__(
        self,
        config: PayPalConfig,
        transport: Transport = urllib_transport,
        base_url: str = SANDBOX_URL,
    ) -> None:
        if base_url != SANDBOX_URL or config.env != "sandbox":
            raise PayPalError("Only the PayPal sandbox is supported.")
        self._config = config
        self._transport = transport
        self._base = base_url
        self._token: str | None = None
        self._token_expiry = 0.0

    def __repr__(self) -> str:
        return f"PayPalClient(env={self._config.env!r})"

    # -- plumbing ----------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(
            f"{self._config.client_id}:{self._config.client_secret}".encode()
        ).decode()
        status, body = self._transport(
            "POST",
            f"{self._base}/v1/oauth2/token",
            {
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            b"grant_type=client_credentials",
        )
        if status != 200 or "access_token" not in body:
            raise PayPalError("PayPal rejected the sandbox credentials.", status)
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 300))
        return self._token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        *,
        request_id: str | None = None,
        ok: tuple[int, ...] = (200, 201, 204),
        raw: tuple[bytes, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": raw[1] if raw else "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        if extra_headers:
            headers.update(extra_headers)
        body = raw[0] if raw else (json.dumps(payload).encode() if payload is not None else None)
        status, data = self._transport(method, f"{self._base}{path}", headers, body)
        if status not in ok:
            if not isinstance(data, dict):
                data = {}
            details = data.get("details")
            issues = tuple(d["issue"] for d in details if isinstance(d, dict)
                           and isinstance(d.get("issue"), str)) if isinstance(details, list) else ()
            raise PayPalError(
                f"PayPal returned {status} for {method} {path.split('?')[0]}: "
                f"{data.get('name', 'error')}",
                status,
                data.get("debug_id"),
                issues=issues,
            )
        return data

    def check_credentials(self) -> bool:
        self._access_token()
        return True

    # -- orders v2 ---------------------------------------------------------

    def create_order(
        self,
        *,
        items: list[dict],
        currency: str,
        mandate_id: str,
        reference_id: str,
        description: str = "",
        request_id: str | None = None,
    ) -> dict:
        """Create a CAPTURE order. ``items`` are guard line-item dicts."""
        total = sum(i["unit_price_minor"] * i["quantity"] for i in items)
        order = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id[:256],
                    "custom_id": mandate_id[:255],
                    "description": description[:127],
                    "amount": {
                        "currency_code": currency,
                        "value": format_amount(total, currency),
                        "breakdown": {
                            "item_total": {
                                "currency_code": currency,
                                "value": format_amount(total, currency),
                            }
                        },
                    },
                    "items": [
                        {
                            "name": i["name"][:127],
                            "quantity": str(i["quantity"]),
                            "unit_amount": {
                                "currency_code": currency,
                                "value": format_amount(i["unit_price_minor"], currency),
                            },
                            "category": "PHYSICAL_GOODS",
                        }
                        for i in items
                    ],
                }
            ],
        }
        return self._request("POST", "/v2/checkout/orders", order, request_id=request_id)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/v2/checkout/orders/{_resource_id(order_id)}")

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict:
        return self._request(
            "POST",
            f"/v2/checkout/orders/{_resource_id(order_id)}/capture",
            {},
            request_id=request_id,
            extra_headers={"Prefer": "return=representation"},
        )

    def refund_capture(
        self, capture_id: str, minor: int, currency: str, request_id: str | None = None
    ) -> dict:
        return self._request(
            "POST",
            f"/v2/payments/captures/{_resource_id(capture_id)}/refund",
            {"amount": {"currency_code": currency, "value": format_amount(minor, currency)}},
            request_id=request_id,
            extra_headers={"Prefer": "return=representation"},
        )

    def get_refund(self, refund_id: str) -> dict:
        return self._request("GET", f"/v2/payments/refunds/{_resource_id(refund_id)}")

    # -- disputes ----------------------------------------------------------

    def list_disputes(self, page_size: int = 10) -> dict:
        return self._request("GET", f"/v1/customer/disputes?page_size={int(page_size)}")

    def get_dispute(self, dispute_id: str) -> dict:
        return self._request("GET", f"/v1/customer/disputes/{_resource_id(dispute_id)}")

    def provide_evidence(self, dispute_id: str, payload: dict) -> dict:
        return self._request(
            "POST",
            f"/v1/customer/disputes/{_resource_id(dispute_id)}/provide-evidence",
            payload,
            ok=(200, 201, 202, 204),
        )

    def provide_evidence_with_document(
        self, dispute_id: str, payload: dict, filename: str, content: bytes
    ) -> dict:
        """Multipart variant: JSON ``input`` part plus one attached file."""
        types = {"pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "png": "image/png", "gif": "image/gif"}
        mime = types.get(filename.rsplit(".", 1)[-1].lower())
        if not mime or any(c in filename for c in '\r\n"/\\'):
            raise PayPalError("Evidence requires a safe image or PDF filename; JSON is not supported.")
        if not content or len(content) >= 10_000_000:
            raise PayPalError("Evidence file must be nonempty and smaller than 10 MB.")
        boundary = "afr" + digest_boundary(content)
        crlf = b"\r\n"
        parts = [
            b"--" + boundary.encode(), b'Content-Disposition: form-data; name="input"',
            b"Content-Type: application/json", b"", json.dumps(payload).encode(),
            b"--" + boundary.encode(),
            f'Content-Disposition: form-data; name="file1"; filename="{filename}"'.encode(),
            f"Content-Type: {mime}".encode(), b"", content,
            b"--" + boundary.encode() + b"--", b"",
        ]
        return self._request(
            "POST",
            f"/v1/customer/disputes/{_resource_id(dispute_id)}/provide-evidence",
            ok=(200, 201, 202, 204),
            raw=(crlf.join(parts), f"multipart/form-data; boundary={boundary}"),
        )

    def create_dispute(self, *, buyer_transaction_id: str, reason: str, amount_minor: int,
                       currency: str, buyer_payer_id: str, note: str = "") -> dict:
        """Sandbox: open a dispute as the buyer. PayPal requires an auth
        assertion naming the buyer; it is an unsigned JWT built from the
        app's client id and the buyer's payer id."""
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
        claims = base64.urlsafe_b64encode(json.dumps(
            {"iss": self._config.client_id, "payer_id": buyer_payer_id}).encode()).rstrip(b"=")
        assertion = (header + b"." + claims + b".").decode()
        payload = {
            "disputed_transactions": [{"buyer_transaction_id": buyer_transaction_id}],
            "reason": reason,
            "dispute_amount": {"currency_code": currency, "value": format_amount(amount_minor, currency)},
            "extensions": {"merchandize_dispute_properties": {"issue_type": "PRODUCT"}},
        }
        if note:
            payload["messages"] = [{"posted_by": "BUYER", "content": note[:2000]}]
        headers = {"PayPal-Auth-Assertion": assertion}
        try:
            return self._request("POST", "/v1/customer/disputes", payload, extra_headers=headers)
        except PayPalError as error:
            # PayPal's docs show both JSON and multipart for this call; if JSON is refused as
            # an unsupported media type, send the same payload as the multipart "input" part.
            if error.status not in (415, 400):
                raise
        boundary = "afr" + digest_boundary(json.dumps(payload).encode())
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="input"\r\n'
                f"Content-Type: application/json\r\n\r\n{json.dumps(payload)}\r\n--{boundary}--\r\n").encode()
        return self._request("POST", "/v1/customer/disputes", extra_headers=headers,
                             raw=(body, f"multipart/form-data; boundary={boundary}"))

    def require_evidence(self, dispute_id: str, action: str = "SELLER_EVIDENCE") -> dict:
        """Sandbox-only helper: move a dispute into the evidence stage."""
        return self._request(
            "POST",
            f"/v1/customer/disputes/{_resource_id(dispute_id)}/require-evidence",
            {"action": action},
        )

    def adjudicate(self, dispute_id: str, outcome: str) -> dict:
        """Sandbox-only helper: settle a dispute (BUYER_FAVOR or SELLER_FAVOR)."""
        return self._request(
            "POST",
            f"/v1/customer/disputes/{_resource_id(dispute_id)}/adjudicate",
            {"adjudication_outcome": outcome},
        )

    def create_webhook(self, url: str, event_types: list[str]) -> dict:
        return self._request(
            "POST", "/v1/notifications/webhooks",
            {"url": url, "event_types": [{"name": n} for n in event_types]},
        )

    # -- webhooks ----------------------------------------------------------

    def verify_webhook_signature(
        self, *, webhook_id: str, headers: Mapping[str, str], event: dict
    ) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        try:
            body = {
                "auth_algo": lower["paypal-auth-algo"],
                "cert_url": lower["paypal-cert-url"],
                "transmission_id": lower["paypal-transmission-id"],
                "transmission_sig": lower["paypal-transmission-sig"],
                "transmission_time": lower["paypal-transmission-time"],
                "webhook_id": webhook_id,
                "webhook_event": event,
            }
        except KeyError:
            return False
        data = self._request("POST", "/v1/notifications/verify-webhook-signature", body)
        return data.get("verification_status") == "SUCCESS"
