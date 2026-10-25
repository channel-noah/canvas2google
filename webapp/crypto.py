"""Encryption for secrets stored in the database (feed URLs, Google tokens).

Uses Fernet from the `cryptography` library: AES-128 encryption plus an HMAC
signature, so stored data can't be read *or* tampered with without the key.
The key lives outside the database (APP_ENCRYPTION_KEY), so a stolen database
file alone reveals nothing.
"""

from __future__ import annotations

import hmac
import json

from cryptography.fernet import Fernet, InvalidToken

from canvas_sync.config import ConfigError


class SecretError(Exception):
    """A stored secret couldn't be decrypted or didn't belong to this record."""


def secret_context(user_id: int, name: str) -> str:
    return f"user:{user_id}:{name}"


class SecretBox:
    def __init__(self, key: str):
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError):
            raise ConfigError("APP_ENCRYPTION_KEY is not a valid Fernet key.") from None

    def encrypt(self, value: str, context: str) -> bytes:
        # The context (e.g. "user:3:feed_url") is sealed inside the ciphertext.
        # That stops someone with database access from swapping one user's
        # encrypted value into another user's row: decryption would then fail.
        payload = json.dumps({"ctx": context, "v": value})
        return self._fernet.encrypt(payload.encode("utf-8"))

    def decrypt(self, token: bytes, context: str) -> str:
        try:
            payload = json.loads(self._fernet.decrypt(token))
        except (InvalidToken, ValueError, TypeError):
            raise SecretError("A stored secret couldn't be decrypted.") from None
        if not isinstance(payload, dict) or not hmac.compare_digest(str(payload.get("ctx", "")), context):
            raise SecretError("A stored secret doesn't belong to this record.")
        return payload["v"]
