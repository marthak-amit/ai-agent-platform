"""Google ID-token verification: a real RSA key signs real JWTs; only Google's JWKS download is stubbed."""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from app.config import Settings
from app.services import google_auth_service as gas

CLIENT_ID = "123-abc.apps.googleusercontent.com"
KID = "test-key-1"


def _make_key() -> tuple[str, dict]:
    """(PEM private key, public JWK dict) for a fresh RSA key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public = jwk.construct(public_pem, "RS256").to_dict()
    public = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in public.items()}
    public.update(kid=KID, use="sig", alg="RS256")
    return pem, public


@pytest.fixture
def signer(monkeypatch):
    """Configure GOOGLE_CLIENT_ID, serve a fixed JWKS instead of calling Google, and return a token factory."""
    pem, public = _make_key()
    monkeypatch.setattr(gas, "get_settings", lambda: Settings(google_client_id=CLIENT_ID))
    fetched = {"n": 0}

    async def fake_fetch():
        fetched["n"] += 1
        now = time.monotonic()
        gas._jwks_cache.update(keys=[public], expires=now + 3600, fetched=now)
        return [public]

    monkeypatch.setattr(gas, "_fetch_jwks", fake_fetch)
    gas.reset_cache()

    def make(**over):
        claims = {
            "iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1100", "email": "owner@shop.in",
            "email_verified": True, "name": "Owner", "iat": int(time.time()), "exp": int(time.time()) + 3600,
        }
        claims.update(over)
        return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": KID})

    make.fetched = fetched
    yield make
    gas.reset_cache()


async def test_valid_token_returns_identity(signer):
    """A correctly signed token for our client id yields the Google identity."""
    ident = await gas.verify_id_token(signer())
    assert (ident.sub, ident.email, ident.name) == ("1100", "owner@shop.in", "Owner")


async def test_keys_are_cached_between_calls(signer):
    """Google's key set is downloaded once and reused."""
    await gas.verify_id_token(signer())
    await gas.verify_id_token(signer())
    assert signer.fetched["n"] == 1


@pytest.mark.parametrize("claim, value", [
    ("aud", "someone-elses-app.apps.googleusercontent.com"),     # token minted for another application
    ("iss", "https://evil.example"),
    ("exp", int(time.time()) - 10),
    ("email_verified", False),
    ("email", ""),
])
async def test_bad_claims_are_rejected(signer, claim, value):
    """Wrong audience / issuer, expired, unverified or missing email: never accepted."""
    with pytest.raises(gas.GoogleAuthError):
        await gas.verify_id_token(signer(**{claim: value}))


async def test_token_signed_by_another_key_is_rejected(signer):
    """A valid-looking token signed with a different private key fails signature verification."""
    other_pem, _ = _make_key()
    forged = jwt.encode(
        {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1", "email": "a@b.c", "email_verified": True,
         "exp": int(time.time()) + 600},
        other_pem, algorithm="RS256", headers={"kid": KID},
    )
    with pytest.raises(gas.GoogleAuthError):
        await gas.verify_id_token(forged)


async def test_hs256_token_is_rejected(signer):
    """alg=none / HS256 downgrade attempts are refused before any key lookup."""
    forged = jwt.encode({"aud": CLIENT_ID, "sub": "1", "email": "a@b.c", "email_verified": True}, "secret", algorithm="HS256")
    with pytest.raises(gas.GoogleAuthError):
        await gas.verify_id_token(forged)


async def test_garbage_is_rejected(signer):
    """Not a JWT at all."""
    with pytest.raises(gas.GoogleAuthError):
        await gas.verify_id_token("not-a-jwt")


async def test_unconfigured_server_refuses(monkeypatch):
    """No GOOGLE_CLIENT_ID: a dedicated error (the router maps it to 503), and is_enabled() is False."""
    monkeypatch.setattr(gas, "get_settings", lambda: Settings(google_client_id=""))
    assert gas.is_enabled() is False
    with pytest.raises(gas.GoogleNotConfiguredError):
        await gas.verify_id_token("x.y.z")


async def test_unknown_kid_cannot_force_a_key_download_per_request(signer):
    """Tokens with random key ids are rejected without hammering Google: at most one refetch per minute."""
    await gas.verify_id_token(signer())                       # warms the cache (1 download)
    pem, _ = _make_key()
    for i in range(5):
        forged = jwt.encode(
            {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1", "email": "a@b.c",
             "email_verified": True, "exp": int(time.time()) + 600},
            pem, algorithm="RS256", headers={"kid": f"random-{i}"},
        )
        with pytest.raises(gas.GoogleAuthError):
            await gas.verify_id_token(forged)
    assert signer.fetched["n"] == 1
