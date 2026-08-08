"""Unit tests for the cryptographic primitives.

These are the pieces where a silent bug is most damaging: a broken encrypt/
decrypt round-trip would only surface when an OAuth token failed to replay,
long after the token was written.
"""

from __future__ import annotations

import threading
import uuid

import pytest

from app.core.security import (
    decrypt_secret,
    encrypt_secret,
    generate_session_token,
    hash_password,
    hash_session_token,
    uuid7,
    verify_password,
)


class TestPasswordHashing:
    def test_round_trip(self) -> None:
        h = hash_password("a-sufficiently-long-password")
        assert verify_password("a-sufficiently-long-password", h)

    def test_rejects_wrong_password(self) -> None:
        h = hash_password("a-sufficiently-long-password")
        assert not verify_password("wrong-password-entirely", h)

    def test_hash_is_salted(self) -> None:
        """Identical passwords must not produce identical hashes."""
        assert hash_password("same-password-here") != hash_password("same-password-here")

    def test_plaintext_absent_from_hash(self) -> None:
        secret = "my-very-secret-passphrase"
        assert secret not in hash_password(secret)

    def test_malformed_hash_is_rejected_not_raised(self) -> None:
        assert not verify_password("anything", "not-a-valid-argon2-hash")


class TestSecretEncryption:
    def test_round_trip(self) -> None:
        token = "x-oauth-access-token-value"  # noqa: S105
        assert decrypt_secret(encrypt_secret(token)) == token

    def test_ciphertext_differs_each_time(self) -> None:
        """A fresh nonce per encryption; identical plaintext must not collide."""
        a = encrypt_secret("identical-token-value")
        b = encrypt_secret("identical-token-value")
        assert a != b
        assert decrypt_secret(a) == decrypt_secret(b) == "identical-token-value"

    def test_plaintext_not_recoverable_from_ciphertext(self) -> None:
        assert "recognisable-token" not in encrypt_secret("recognisable-token")

    def test_tampering_is_detected(self) -> None:
        """GCM authentication must reject a modified ciphertext."""
        ct = encrypt_secret("authentic-token")
        tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
        with pytest.raises(ValueError):
            decrypt_secret(tampered)

    def test_empty_secret_refused(self) -> None:
        with pytest.raises(ValueError):
            encrypt_secret("")


class TestSessionTokens:
    def test_tokens_are_unique_and_long(self) -> None:
        tokens = {generate_session_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(len(t) >= 43 for t in tokens)

    def test_hash_is_deterministic_and_hides_token(self) -> None:
        token = generate_session_token()
        assert hash_session_token(token) == hash_session_token(token)
        assert token not in hash_session_token(token)
        assert len(hash_session_token(token)) == 64


class TestUUID7:
    def test_is_version_7(self) -> None:
        assert uuid7().version == 7

    def test_is_time_sortable(self) -> None:
        """Sequential ids must sort in creation order — this is why we use v7."""
        ids = [uuid7() for _ in range(50)]
        assert ids == sorted(ids, key=lambda u: u.int)

    def test_monotonic_within_one_millisecond(self) -> None:
        """Regression: ids minted in the same millisecond must still sort.

        A naive v7 fills rand_a with random bits, so same-millisecond ids
        tie-break arbitrarily and the sequential-insert property is lost. The
        counter in rand_a is what prevents that, and this batch is generated far
        faster than the clock ticks.
        """
        ids = [uuid7() for _ in range(2000)]
        assert ids == sorted(ids, key=lambda u: u.int)

    def test_monotonic_across_threads(self) -> None:
        """Concurrent minting must not produce duplicates."""
        results: list[uuid.UUID] = []
        lock = threading.Lock()

        def worker() -> None:
            batch = [uuid7() for _ in range(500)]
            with lock:
                results.extend(batch)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == len(results) == 2000

    def test_unique(self) -> None:
        assert len({uuid7() for _ in range(1000)}) == 1000

    def test_variant_is_rfc4122(self) -> None:
        assert uuid7().variant == uuid.RFC_4122
