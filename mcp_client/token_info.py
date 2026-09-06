"""Bearer-token introspection.

The cTrader MCP token is an opaque, JWT-shaped string whose payload carries the
plant and, critically, ``"environment": "demo" | "live"``.  The bot decodes it at
start-up purely so the operator can never be in doubt about which account is
about to be traded - it is logged loudly and used as a live-trading safety gate.

The signature is never verified here: we are not authenticating anyone, only
reading a self-describing claim for our own safety checks.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from utils.logging import get_logger

log = get_logger("token")

#: Claim values that must never be echoed into logs.
_SENSITIVE_CLAIMS = {"token", "accessToken", "access_token", "secret", "password", "refreshToken"}


@dataclass(frozen=True)
class TokenInfo:
    environment: Optional[str]      # "demo" | "live" | None if undetectable
    plant: Optional[str]            # e.g. "icmarkets"
    claims: dict[str, Any] = field(default_factory=dict)
    decoded: bool = False

    @property
    def is_demo(self) -> bool:
        return (self.environment or "").lower() == "demo"

    @property
    def is_live(self) -> bool:
        return (self.environment or "").lower() == "live"

    @property
    def is_unknown(self) -> bool:
        return self.environment is None

    def safe_claims(self) -> dict[str, Any]:
        """Claims with credential-ish values masked, safe for logs/Telegram."""
        out: dict[str, Any] = {}
        for key, value in self.claims.items():
            if key in _SENSITIVE_CLAIMS or (isinstance(value, str) and len(value) > 48):
                out[key] = "***"
            else:
                out[key] = value
        return out

    def describe(self) -> str:
        if not self.decoded:
            return "environment=UNKNOWN (token payload not decodable)"
        return (
            f"environment={(self.environment or 'UNKNOWN').upper()} "
            f"plant={self.plant or 'unknown'}"
        )


def _b64_json(segment: str) -> Optional[dict[str, Any]]:
    """Decode one base64url segment into a JSON object, or None."""
    if not segment:
        return None
    padded = segment + "=" * (-len(segment) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            raw = decoder(padded)
        except (binascii.Error, ValueError):
            continue
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def decode_token(token: str) -> TokenInfo:
    """Best-effort decode of the Bearer token's self-describing claims.

    Handles both the JWT layout (``header.payload.signature``) and a bare
    base64-encoded JSON blob, and never raises: an undecodable token is reported
    as ``environment=None`` and handled by the caller's safety gate.
    """
    token = (token or "").strip()
    if not token:
        return TokenInfo(None, None)

    claims: dict[str, Any] = {}
    segments = token.split(".")
    # Try each segment; the payload is usually the second one but bare-blob
    # tokens have exactly one, so scanning all of them is both simple and safe.
    for segment in segments:
        payload = _b64_json(segment)
        if payload:
            claims.update(payload)

    if not claims:
        log.warning("Bearer token payload could not be decoded - environment unknown.")
        return TokenInfo(None, None)

    environment = claims.get("environment") or claims.get("env")
    plant = claims.get("plant") or claims.get("broker")
    if isinstance(environment, str):
        environment = environment.strip().lower()
    else:
        environment = None

    return TokenInfo(
        environment=environment,
        plant=str(plant) if plant else None,
        claims=claims,
        decoded=True,
    )
