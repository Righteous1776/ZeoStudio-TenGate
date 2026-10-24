from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .integrity import canonical_json
from .models import ActorSeal


class SealVerifier(Protocol):
    def verify(self, seal: ActorSeal) -> bool: ...


class HmacSealAuthority:
    """Local seal issuer for trusted gateways and deterministic test hosts.

    Production adapters should keep the key outside task/message storage and
    issue a seal only after their upstream identity check succeeds.
    """

    def __init__(self, key: bytes, *, issuer: str) -> None:
        if len(key) < 32:
            raise ValueError("seal key must contain at least 32 bytes")
        if not issuer:
            raise ValueError("issuer is required")
        self._key = bytes(key)
        self.issuer = issuer

    @classmethod
    def ephemeral(cls, *, issuer: str = "local-ephemeral") -> HmacSealAuthority:
        return cls(secrets.token_bytes(32), issuer=issuer)

    def issue(
        self,
        subject: str,
        *,
        roles: frozenset[str] = frozenset(),
        lane: str | None = None,
        ttl_seconds: int = 300,
    ) -> ActorSeal:
        if not subject:
            raise ValueError("subject is required")
        if any(not role or not isinstance(role, str) for role in roles):
            raise ValueError("roles must be non-empty strings")
        if lane is not None and not lane:
            raise ValueError("lane must be non-empty when supplied")
        if ttl_seconds < 1 or ttl_seconds > 86_400:
            raise ValueError("ttl_seconds must be between 1 and 86400")
        issued = datetime.now(UTC)
        unsigned = ActorSeal(
            subject=subject,
            roles=frozenset(roles),
            lane=lane,
            issuer=self.issuer,
            issued_at=issued.isoformat(),
            expires_at=(issued + timedelta(seconds=ttl_seconds)).isoformat(),
            nonce=secrets.token_hex(16),
        )
        return ActorSeal(
            subject=unsigned.subject,
            roles=unsigned.roles,
            lane=unsigned.lane,
            issuer=unsigned.issuer,
            issued_at=unsigned.issued_at,
            expires_at=unsigned.expires_at,
            nonce=unsigned.nonce,
            proof=self._sign(unsigned),
        )

    def verify(self, seal: ActorSeal) -> bool:
        try:
            expires = datetime.fromisoformat(seal.expires_at)
            issued = datetime.fromisoformat(seal.issued_at)
        except (TypeError, ValueError):
            return False
        now = datetime.now(UTC)
        if issued.tzinfo is None or expires.tzinfo is None:
            return False
        if not seal.subject or seal.issuer != self.issuer or issued > now or expires <= now or expires <= issued:
            return False
        try:
            return hmac.compare_digest(seal.proof, self._sign(seal))
        except (TypeError, ValueError):
            return False

    def _sign(self, seal: ActorSeal) -> str:
        body = canonical_json(seal.unsigned_fields()).encode("utf-8")
        return hmac.new(self._key, body, hashlib.sha256).hexdigest()

