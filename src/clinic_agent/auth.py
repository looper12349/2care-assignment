"""Small signed identity envelopes for the loopback-only synthetic demonstration.

Production deployments should exchange verified identity-provider tokens. No
patient identity is accepted from model output or an unverified browser header.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from clinic_agent.models import Principal


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _secret(secret: str | None) -> str:
    value = secret or os.getenv("CLINIC_SERVICE_TOKEN", "")
    if len(value) < 32:
        raise ValueError("Configure a service secret of at least 32 characters.")
    return value


def sign_context(
    principal: Principal,
    secret: str | None = None,
    *,
    audience: str = "scheduling",
    expires_in: int = 60,
) -> str:
    payload = _encode(
        json.dumps(
            {
                "principal": principal.model_dump(),
                "aud": audience,
                "exp": int(time.time()) + expires_in,
                "iss": "carepath-demo",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    signature = _encode(
        hmac.new(_secret(secret).encode(), payload.encode(), hashlib.sha256).digest()
    )
    return f"{payload}.{signature}"


def verify_context(
    token: str, secret: str | None = None, *, audience: str = "scheduling"
) -> Principal:
    try:
        if len(token) > 4096:
            raise ValueError("Invalid identity envelope.")
        payload, signature = token.split(".")
        expected = hmac.new(_secret(secret).encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _decode(signature)):
            raise ValueError("Invalid signature.")
        decoded = json.loads(_decode(payload))
        if (
            decoded["aud"] != audience
            or decoded["iss"] != "carepath-demo"
            or decoded["exp"] <= time.time()
        ):
            raise ValueError("Expired or incorrectly scoped identity.")
        return Principal.model_validate(decoded["principal"])
    except Exception as exc:
        raise ValueError("Invalid or expired identity.") from exc
