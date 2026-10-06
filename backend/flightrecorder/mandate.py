"""Mandates: what a person has allowed an AI agent to buy.

A mandate is a narrow, expiring permission. It names one agent, a budget,
a per-order cap, the merchants and categories the agent may buy from, how
many orders it may place and when the permission ends. The person's key
signs the canonical form, so anyone holding the public key can later prove
exactly what was authorised, and that it has not been edited since.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .canonical import canonical_json, digest

MANDATE_VERSION = 1
ANY_CATEGORY = "*"
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class MandateError(ValueError):
    """Raised when a mandate is malformed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(moment: datetime) -> str:
    """Timezone-aware datetime to a fixed-format UTC string."""
    if moment.tzinfo is None:
        raise MandateError("datetimes must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _normalise(values: Iterable[str], what: str) -> tuple[str, ...]:
    cleaned = sorted({v.strip().lower() for v in values if v and v.strip()})
    if not cleaned:
        raise MandateError(f"at least one {what} is required")
    return tuple(cleaned)


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    principal_id: str
    agent_id: str
    purpose: str
    currency: str
    total_budget_minor: int
    per_order_cap_minor: int
    approval_threshold_minor: int | None
    allowed_merchants: tuple[str, ...]
    allowed_categories: tuple[str, ...]
    max_orders: int
    created_at: str
    expires_at: str
    version: int = MANDATE_VERSION

    def to_payload(self) -> dict:
        """The exact data that is signed."""
        return {
            "version": self.version,
            "mandate_id": self.mandate_id,
            "principal_id": self.principal_id,
            "agent_id": self.agent_id,
            "purpose": self.purpose,
            "currency": self.currency,
            "total_budget_minor": self.total_budget_minor,
            "per_order_cap_minor": self.per_order_cap_minor,
            "approval_threshold_minor": self.approval_threshold_minor,
            "allowed_merchants": list(self.allowed_merchants),
            "allowed_categories": list(self.allowed_categories),
            "max_orders": self.max_orders,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def digest(self) -> str:
        return digest(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict) -> "Mandate":
        return cls(
            mandate_id=payload["mandate_id"],
            principal_id=payload["principal_id"],
            agent_id=payload["agent_id"],
            purpose=payload["purpose"],
            currency=payload["currency"],
            total_budget_minor=payload["total_budget_minor"],
            per_order_cap_minor=payload["per_order_cap_minor"],
            approval_threshold_minor=payload["approval_threshold_minor"],
            allowed_merchants=tuple(payload["allowed_merchants"]),
            allowed_categories=tuple(payload["allowed_categories"]),
            max_orders=payload["max_orders"],
            created_at=payload["created_at"],
            expires_at=payload["expires_at"],
            version=payload.get("version", MANDATE_VERSION),
        )


def new_mandate(
    *,
    principal_id: str,
    agent_id: str,
    purpose: str,
    currency: str,
    total_budget_minor: int,
    per_order_cap_minor: int,
    allowed_merchants: Iterable[str],
    allowed_categories: Iterable[str],
    expires_at: datetime,
    max_orders: int = 1,
    approval_threshold_minor: int | None = None,
    created_at: datetime | None = None,
    mandate_id: str | None = None,
) -> Mandate:
    """Build a mandate, refusing anything that is not least-privilege."""
    created = created_at or utc_now()

    if not principal_id.strip() or not agent_id.strip():
        raise MandateError("principal_id and agent_id are required")
    if not _CURRENCY.match(currency):
        raise MandateError("currency must be a three-letter uppercase code, e.g. USD")
    for name, amount in (
        ("total_budget_minor", total_budget_minor),
        ("per_order_cap_minor", per_order_cap_minor),
        ("max_orders", max_orders),
    ):
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise MandateError(f"{name} must be a positive integer")
    if per_order_cap_minor > total_budget_minor:
        raise MandateError("per_order_cap_minor cannot exceed total_budget_minor")
    if approval_threshold_minor is not None:
        if (
            isinstance(approval_threshold_minor, bool)
            or not isinstance(approval_threshold_minor, int)
            or approval_threshold_minor < 0
        ):
            raise MandateError("approval_threshold_minor must be a non-negative integer")
    if created.tzinfo is None or expires_at.tzinfo is None:
        raise MandateError("created_at and expires_at must be timezone-aware")
    if expires_at <= created:
        raise MandateError("expires_at must be after created_at")

    merchants = _normalise(allowed_merchants, "allowed merchant")
    if ANY_CATEGORY in merchants:
        raise MandateError("merchants must be named explicitly; a wildcard is not allowed")

    return Mandate(
        mandate_id=mandate_id or str(uuid.uuid4()),
        principal_id=principal_id.strip(),
        agent_id=agent_id.strip(),
        purpose=purpose.strip(),
        currency=currency,
        total_budget_minor=total_budget_minor,
        per_order_cap_minor=per_order_cap_minor,
        approval_threshold_minor=approval_threshold_minor,
        allowed_merchants=merchants,
        allowed_categories=_normalise(allowed_categories, "allowed category"),
        max_orders=max_orders,
        created_at=to_iso(created),
        expires_at=to_iso(expires_at),
    )


@dataclass(frozen=True)
class SignedMandate:
    mandate: Mandate
    signature: str  # hex
    public_key: str  # hex, raw Ed25519 public key

    def to_dict(self) -> dict:
        return {
            "mandate": self.mandate.to_payload(),
            "signature": self.signature,
            "public_key": self.public_key,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SignedMandate":
        return cls(
            mandate=Mandate.from_payload(data["mandate"]),
            signature=data["signature"],
            public_key=data["public_key"],
        )


def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    """Return a new private key and its public key as hex."""
    private_key = Ed25519PrivateKey.generate()
    return private_key, public_key_hex(private_key)


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        .hex()
    )


def sign_mandate(mandate: Mandate, private_key: Ed25519PrivateKey) -> SignedMandate:
    signature = private_key.sign(canonical_json(mandate.to_payload()))
    return SignedMandate(
        mandate=mandate,
        signature=signature.hex(),
        public_key=public_key_hex(private_key),
    )


def verify_mandate(signed: SignedMandate, trusted_public_key: str | None = None) -> bool:
    """True only if the signature matches the mandate's current contents.

    If ``trusted_public_key`` is given, the mandate must also be signed by
    that exact key. Without it, a forger could sign an edited mandate with
    a key of their own, so callers that know the person's key should pass it.
    """
    if trusted_public_key is not None and signed.public_key != trusted_public_key:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(signed.public_key))
        public_key.verify(
            bytes.fromhex(signed.signature), canonical_json(signed.mandate.to_payload())
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True
