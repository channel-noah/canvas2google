"""Encryption for secrets stored in the database (feed URLs, Google tokens).

Uses Fernet from the `cryptography` library: AES-128 encryption plus an HMAC
signature, so stored data can't be read *or* tampered with without the key.
The key lives outside the database (APP_ENCRYPTION_KEY), so a stolen database
file alone reveals nothing.

Key rotation: APP_ENCRYPTION_KEY may list several keys, newest first
("NEW,OLD"). New data is encrypted with the first key; any listed key can
decrypt. `rotate` re-encrypts old data under the newest key, after which the
old key can be removed.
"""

from __future__ import annotations

import hmac
import json

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from canvas_sync.config import ConfigError


class SecretError(Exception):
    """A stored secret couldn't be decrypted or didn't belong to this record."""


def secret_context(user_id: int, name: str) -> str:
    return f"user:{user_id}:{name}"


class SecretBox:
    def __init__(self, keys: str):
        parts = [k.strip() for k in keys.split(",") if k.strip()]
        try:
            fernets = [Fernet(k) for k in parts]
        except (ValueError, TypeError):
            raise ConfigError("APP_ENCRYPTION_KEY contains a key that isn't a valid Fernet key.") from None
        if not fernets:
            raise ConfigError("APP_ENCRYPTION_KEY is empty.")
        self._primary = fernets[0]
        self._all = MultiFernet(fernets)

    def encrypt(self, value: str, context: str) -> bytes:
        # The context (e.g. "user:3:feed_url") is sealed inside the ciphertext.
        # That stops someone with database access from swapping one user's
        # encrypted value into another user's row: decryption would then fail.
        payload = json.dumps({"ctx": context, "v": value})
        return self._all.encrypt(payload.encode("utf-8"))  # always uses the first (newest) key

    def decrypt(self, token: bytes, context: str) -> str:
        try:
            payload = json.loads(self._all.decrypt(token))
        except (InvalidToken, ValueError, TypeError):
            raise SecretError("A stored secret couldn't be decrypted.") from None
        if not isinstance(payload, dict) or not hmac.compare_digest(str(payload.get("ctx", "")), context):
            raise SecretError("A stored secret doesn't belong to this record.")
        return payload["v"]

    def needs_rotation(self, token: bytes) -> bool:
        """True if this value was encrypted with an older key."""
        try:
            self._primary.decrypt(token)
            return False
        except InvalidToken:
            return True

    def rotate(self, token: bytes) -> bytes:
        """Re-encrypt under the newest key. The plaintext, including its context
        binding, is unchanged. Raises SecretError if no listed key can read it."""
        try:
            return self._all.rotate(token)
        except InvalidToken:
            raise SecretError("A stored secret couldn't be decrypted with any configured key.") from None
