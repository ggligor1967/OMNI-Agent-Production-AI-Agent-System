import base64
import os
import sys

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


@pytest.fixture
def crypto_utils(tmp_path):
    from agent.crypto_utils import CryptoUtils

    return CryptoUtils(db_path=str(tmp_path / "crypto.db"))


def test_hash_helpers_cover_safe_algorithms_and_fingerprint() -> None:
    from agent.crypto_utils import (
        b64url_decode,
        b64url_encode,
        fingerprint,
        hash_blake2b,
        hash_sha256,
        hash_sha512,
    )

    raw = b"\x00\xffphase-3.7"
    encoded = b64url_encode(raw)

    assert "=" not in encoded
    assert b64url_decode(encoded) == raw

    sha256_hex = hash_sha256("hello")
    sha256_b64 = hash_sha256("hello", hex_out=False)
    sha512_hex = hash_sha512(b"hello")
    blake2b_hex = hash_blake2b("hello", digest_size=16)

    assert len(sha256_hex) == 64
    assert len(b64url_decode(sha256_b64)) == 32
    assert len(sha512_hex) == 128
    assert len(blake2b_hex) == 32
    assert fingerprint("hello", length=10) == sha256_hex[:10]


def test_hmac_and_key_derivation_are_deterministic_with_explicit_inputs() -> None:
    from agent.crypto_utils import hkdf, hmac_sha256, hmac_sha512, hmac_verify, pbkdf2

    sig256 = hmac_sha256("key-material", "message")
    sig512 = hmac_sha512("key-material", "message")

    assert len(sig256) == 64
    assert len(sig512) == 128
    assert hmac_verify("key-material", "message", sig256) is True
    assert hmac_verify("wrong-key", "message", sig256) is False

    salt = b"0123456789abcdef"
    key1, derived_salt = pbkdf2("hunter2", salt=salt, iterations=1_000, key_len=16)
    key2, _ = pbkdf2("hunter2", salt=salt, iterations=1_000, key_len=16)
    out1 = hkdf(b"ikm", length=42, salt=b"salt", info=b"context")
    out2 = hkdf(b"ikm", length=42, salt=b"salt", info=b"context")

    assert derived_salt == salt
    assert key1 == key2
    assert out1 == out2
    assert len(out1) == 42


def test_password_hashing_rejects_malformed_payload_without_leaking_secret() -> None:
    from agent.crypto_utils import hash_password, verify_password

    stored = hash_password("hunter2", iterations=1_000)

    assert verify_password("hunter2", stored) is True
    assert verify_password("wrong-pass", stored) is False

    malformed = "not-base64:abc:super-secret-material"
    assert verify_password("hunter2", malformed) is False


def test_aead_wrap_and_unwrap_detect_tampering_without_key_leakage() -> None:
    from agent.crypto_utils import aes_gcm_decrypt, aes_gcm_encrypt, unwrap_key, wrap_key

    key = "phase-3.7-encryption-key"
    enc = aes_gcm_encrypt("secret payload", key)

    assert aes_gcm_decrypt(enc["ciphertext"], enc["nonce"], enc["tag"], key) == b"secret payload"

    with pytest.raises(ValueError, match="Authentication tag mismatch") as excinfo:
        aes_gcm_decrypt(enc["ciphertext"], enc["nonce"], "tampered-tag", key)

    assert key not in str(excinfo.value)

    wrapped = wrap_key(b"0123456789abcdef0123456789abcdef", key)
    assert unwrap_key(wrapped, key) == b"0123456789abcdef0123456789abcdef"

    with pytest.raises(ValueError, match="Authentication tag mismatch") as wrap_exc:
        unwrap_key(wrapped, "wrong-key")

    assert "wrong-key" not in str(wrap_exc.value)


def test_jwt_helpers_reject_invalid_and_expired_tokens_without_secret_leakage() -> None:
    from agent.crypto_utils import jwt_decode, jwt_encode, jwt_verify

    secret = "phase-3.7-jwt-secret"
    token = jwt_encode({"sub": "alice", "scope": "admin"}, secret, exp_s=60)

    payload = jwt_decode(token, secret)
    assert payload["sub"] == "alice"
    assert payload["scope"] == "admin"
    assert jwt_verify(token, secret) is True

    with pytest.raises(ValueError, match="Invalid JWT format") as fmt_exc:
        jwt_decode("not-a-jwt", secret)
    assert secret not in str(fmt_exc.value)

    with pytest.raises(ValueError, match="Invalid JWT signature") as sig_exc:
        jwt_decode(token, "wrong-secret")
    assert "wrong-secret" not in str(sig_exc.value)
    assert token not in str(sig_exc.value)

    expired = jwt_encode({"sub": "alice"}, secret, exp_s=-1)
    with pytest.raises(ValueError, match="JWT has expired"):
        jwt_decode(expired, secret)

    assert jwt_verify(token, "wrong-secret") is False
    assert jwt_verify("not-a-jwt", secret) is False


def test_crypto_utils_facade_covers_operations_and_safe_encodings(crypto_utils) -> None:
    from agent.crypto_utils import b64url_decode

    sha512_sig = crypto_utils.hmac_sign("shared-key", "payload", algo="sha512")
    sha256_sig = crypto_utils.hmac_sign("shared-key", "payload")
    enc = crypto_utils.encrypt("hello world", "shared-key")
    derived = crypto_utils.derive_key(
        "hunter2",
        salt=b"0123456789abcdef",
        iterations=1_000,
    )
    hkdf_out = crypto_utils.hkdf("ikm", length=16, salt="salt", info="info")
    stored = crypto_utils.hash_password("hunter2", iterations=1_000)
    token = crypto_utils.jwt_encode({"role": "admin"}, "signing-secret", exp_s=60)
    hex_token = crypto_utils.random_token(16, "hex")
    base64_token = crypto_utils.random_token(16, "base64")
    urlsafe_token = crypto_utils.random_token(16, "urlsafe")

    assert len(sha512_sig) == 128
    assert crypto_utils.hmac_verify("shared-key", "payload", sha256_sig) is True
    assert crypto_utils.decrypt(enc, "shared-key") == b"hello world"
    assert set(derived) == {"key", "salt"}
    assert b64url_decode(derived["salt"]) == b"0123456789abcdef"
    assert len(b64url_decode(hkdf_out)) == 16
    assert crypto_utils.verify_password("hunter2", stored) is True
    assert crypto_utils.jwt_decode(token, "signing-secret")["role"] == "admin"
    assert crypto_utils.jwt_verify(token, "signing-secret") is True
    assert len(hex_token) == 32
    assert len(base64.b64decode(base64_token)) == 16
    assert len(b64url_decode(urlsafe_token)) == 16
    assert crypto_utils.constant_compare("same", "same") is True
    assert crypto_utils.constant_compare("same", "different") is False

    stats = crypto_utils.stats()
    assert stats["operations"] >= 5
    assert stats["stored_keys"] == 0


def test_custore_tracks_key_registry_and_audit_entries(tmp_path) -> None:
    from agent.crypto_utils import CUStore

    store = CUStore(str(tmp_path / "cu.db"))
    store.store_key("api-key", "sha256", "wrapped-value")
    store.log_op("encrypt")

    stats = store.stats()

    assert stats["stored_keys"] == 1
    assert stats["operations"] == 1


@pytest.mark.asyncio
async def test_crypto_routes_allow_safe_hashes_and_reject_unsafe_algorithms(tmp_path) -> None:
    from agent.crypto_utils import CryptoUtils

    cu = CryptoUtils(db_path=str(tmp_path / "crypto.db"))
    app = web.Application()
    cu.register_routes(app, prefix="/api")

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        ok_resp = await client.post("/api/crypto/hash", json={"algo": "sha256", "data": "hello"})
        ok_body = await ok_resp.json()

        blake_resp = await client.post("/api/crypto/hash", json={"algo": "blake2b", "data": "hello"})
        blake_body = await blake_resp.json()

        bad_resp = await client.post("/api/crypto/hash", json={"algo": "md5", "data": "hello"})
        bad_body = await bad_resp.json()
    finally:
        await client.close()

    assert ok_resp.status == 200
    assert ok_body["algo"] == "sha256"
    assert len(ok_body["hash"]) == 64

    assert blake_resp.status == 200
    assert blake_body["algo"] == "blake2b"
    assert len(blake_body["hash"]) == 64

    assert bad_resp.status == 400
    assert "sha256, sha512, or blake2b" in bad_body["error"]


@pytest.mark.asyncio
async def test_crypto_routes_round_trip_encrypt_decrypt_jwt_and_stats(tmp_path) -> None:
    from agent.crypto_utils import CryptoUtils, jwt_decode

    cu = CryptoUtils(db_path=str(tmp_path / "crypto.db"))
    app = web.Application()
    cu.register_routes(app, prefix="")

    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        enc_resp = await client.post("/crypto/encrypt", json={
            "plaintext": "hello world",
            "key": "phase-3.7-route-key",
        })
        enc_body = await enc_resp.json()

        dec_resp = await client.post("/crypto/decrypt", json={
            **enc_body,
            "key": "phase-3.7-route-key",
        })
        dec_body = await dec_resp.json()

        bad_dec_resp = await client.post("/crypto/decrypt", json={
            **enc_body,
            "tag": "tampered-tag",
            "key": "phase-3.7-route-key",
        })
        bad_dec_body = await bad_dec_resp.json()

        jwt_resp = await client.post("/crypto/jwt", json={
            "payload": {"sub": "route-user"},
            "secret": "phase-3.7-signing-secret",
            "exp_s": 60,
        })
        jwt_body = await jwt_resp.json()

        stats_resp = await client.get("/crypto/stats")
        stats_body = await stats_resp.json()
    finally:
        await client.close()

    assert enc_resp.status == 200
    assert set(enc_body) == {"ciphertext", "nonce", "tag"}

    assert dec_resp.status == 200
    assert dec_body == {"plaintext": "hello world"}

    assert bad_dec_resp.status == 400
    assert bad_dec_body["error"] == "Authentication tag mismatch"
    assert "phase-3.7-route-key" not in bad_dec_body["error"]

    assert jwt_resp.status == 200
    payload = jwt_decode(jwt_body["token"], "phase-3.7-signing-secret")
    assert payload["sub"] == "route-user"

    assert stats_resp.status == 200
    assert stats_body["operations"] >= 2
    assert stats_body["stored_keys"] == 0
