"""Password hashing, session tokens, and encryption of third-party credentials.

Three separate concerns deliberately kept apart:

* Passwords      -> Argon2id, one-way. We never need the plaintext back.
* Session tokens -> random opaque tokens, stored as SHA-256 digests so a
                    database leak does not hand over live sessions.
* X OAuth tokens -> AES-256-GCM, reversible, because we must replay them to X.

Note there is no code path anywhere in this project that stores an X *password*.
The OAuth flow means we never see one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import get_settings

# OWASP-recommended baseline; tuned for interactive login latency.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)

_AAD = b"xagent:oauth-token:v1"
_NONCE_BYTES = 12


# --------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, ValueError):
        return True


# ---------------------------------------------------------------------- sessions
def generate_session_token() -> str:
    """Opaque, high-entropy session token handed to the client."""
    return secrets.token_urlsafe(48)


def hash_session_token(token: str) -> str:
    """Only the digest is persisted, so stolen rows cannot be replayed."""
    return hashlib.sha256(token.encode()).hexdigest()


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


# -------------------------------------------------------------------- encryption
def encrypt_secret(plaintext: str) -> str:
    """AES-256-GCM encrypt. Returns base64url(nonce || ciphertext || tag)."""
    if not plaintext:
        raise ValueError("Refusing to encrypt an empty secret.")
    key = get_settings().encryption_key_bytes
    nonce = os.urandom(_NONCE_BYTES)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), _AAD)
    return base64.urlsafe_b64encode(nonce + ct).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Reverse of `encrypt_secret`. Raises ValueError on tampering."""
    key = get_settings().encryption_key_bytes
    try:
        raw = base64.urlsafe_b64decode(ciphertext)
        nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, ct, _AAD).decode()
    except Exception as exc:  # noqa: BLE001
        # Deliberately opaque: never leak whether it was a bad key, a bad nonce
        # or a forged tag.
        raise ValueError("Could not decrypt secret (wrong key or tampered data).") from exc


# --------------------------------------------------------------------------- ids
_uuid_lock = threading.Lock()
_last_ms = 0
_counter = 0

# 12-bit monotonic counter lives in rand_a (RFC 9562 §6.2 method 1).
_COUNTER_MAX = 0xFFF


def uuid7() -> uuid.UUID:
    """Monotonic, time-sortable UUID v7 (RFC 9562).

    Used for every primary key. Sortable ids keep B-tree inserts sequential,
    which matters for the append-only snapshot tables that dominate this
    schema's write volume.

    Millisecond timestamps alone are not enough: this process can mint many ids
    inside one millisecond, and filling `rand_a` with random bits would let
    those tie-break in arbitrary order — losing exactly the insert locality the
    version was chosen for. So `rand_a` carries a counter that increments within
    a millisecond and resets when the clock advances.

    The counter also absorbs a backwards clock step (NTP correction), holding
    the previous timestamp rather than emitting ids that sort into the past.
    """
    global _last_ms, _counter

    with _uuid_lock:
        ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF

        if ms > _last_ms:
            _last_ms, _counter = ms, 0
        else:
            # Same millisecond, or the clock went backwards.
            _counter += 1
            if _counter > _COUNTER_MAX:
                # Overflow: borrow from the next millisecond rather than wrap.
                _last_ms += 1
                _counter = 0
            ms = _last_ms

        counter = _counter

    # 48-bit timestamp | version 7 | 12-bit counter | variant 0b10 | 62 random
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF
    value = ms << 80
    value |= 0x7 << 76
    value |= counter << 64
    value |= 0b10 << 62
    value |= rand_b
    return uuid.UUID(int=value)
