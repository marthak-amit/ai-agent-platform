"""
"Continue with Google": verify a Google Identity Services ID token (the `credential` the Google button returns).

Why not Firebase: there is no Firebase in this stack, so no `*.firebaseapp.com` / raw project domain can ever appear
on the consent screen. The consent screen shows whatever the SellerTalk24 Google Cloud project's OAuth consent screen
says (app name, logo, authorized domains, publishing status "In production" + verified brand) — configure it there
(see CLAUDE.md "Google sign-in"). This module only trusts a token if:

  * the signature verifies against Google's published keys (JWKS, cached, refetched once on an unknown `kid`);
  * `aud` is our GOOGLE_CLIENT_ID (a token minted for another app is rejected) and `iss` is Google;
  * it has not expired;
  * `email` is present and `email_verified` is true — an unverified Google email never creates or links an account.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from jose import JWTError, jwt

from app.config import get_settings

logger = logging.getLogger(__name__)

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_JWKS_DEFAULT_TTL_SECONDS = 3600
#: Never re-download Google's keys more often than this, however many unknown-`kid` tokens arrive (a forged token
#: with a random kid must not turn every request into an outbound call to Google).
_JWKS_MIN_REFETCH_SECONDS = 60

_jwks_cache: dict = {"keys": [], "expires": 0.0, "fetched": float("-inf")}


class GoogleAuthError(Exception):
    """The Google credential is invalid, expired, for another app, or its email is unverified."""


class GoogleNotConfiguredError(GoogleAuthError):
    """GOOGLE_CLIENT_ID is not set on this server."""


@dataclass(frozen=True)
class GoogleIdentity:
    """The verified facts about a Google sign-in."""

    sub: str
    email: str
    name: str | None
    picture: str | None


def is_enabled() -> bool:
    """Whether Google sign-in is configured (GOOGLE_CLIENT_ID set)."""
    return bool(get_settings().google_client_id)


def reset_cache() -> None:
    """Forget the cached Google signing keys (tests)."""
    _jwks_cache.update(keys=[], expires=0.0, fetched=float("-inf"))


async def _fetch_jwks() -> list[dict]:
    """Download Google's current signing keys and cache them for the TTL Google advertises (default 1h)."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.get(GOOGLE_JWKS_URL)
        resp.raise_for_status()
    ttl = _JWKS_DEFAULT_TTL_SECONDS
    for part in resp.headers.get("cache-control", "").split(","):
        part = part.strip()
        if part.startswith("max-age="):
            try:
                ttl = max(60, int(part[len("max-age="):]))
            except ValueError:
                pass
    keys = resp.json().get("keys", [])
    now = time.monotonic()
    _jwks_cache.update(keys=keys, expires=now + ttl, fetched=now)
    return keys


async def _key_for(kid: str) -> dict | None:
    """The Google signing key with id *kid*, refetching the key set once if it is unknown or the cache is stale."""
    now = time.monotonic()
    known = any(k.get("kid") == kid for k in _jwks_cache["keys"])
    stale = now >= _jwks_cache["expires"]
    recently_fetched = now - _jwks_cache["fetched"] < _JWKS_MIN_REFETCH_SECONDS
    if (stale or not known) and not (recently_fetched and not stale):
        try:
            await _fetch_jwks()
        except httpx.HTTPError as exc:
            logger.error("google sign-in: could not fetch Google signing keys: %s", exc)
            if not _jwks_cache["keys"]:
                raise GoogleAuthError("Could not reach Google to verify the sign-in. Please try again.") from exc
    return next((k for k in _jwks_cache["keys"] if k.get("kid") == kid), None)


async def verify_id_token(credential: str) -> GoogleIdentity:
    """
    Verify *credential* and return the Google identity it proves.

    Raises:
        GoogleNotConfiguredError: GOOGLE_CLIENT_ID is unset.
        GoogleAuthError: anything wrong with the token (bad signature, wrong audience/issuer, expired, no verified email).
    """
    client_id = get_settings().google_client_id
    if not client_id:
        raise GoogleNotConfiguredError("Google sign-in is not configured.")
    try:
        header = jwt.get_unverified_header(credential)
    except JWTError as exc:
        raise GoogleAuthError("Invalid Google credential.") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise GoogleAuthError("Invalid Google credential.")
    key = await _key_for(header["kid"])
    if key is None:
        raise GoogleAuthError("Invalid Google credential.")
    try:
        claims = jwt.decode(
            credential, key, algorithms=["RS256"], audience=client_id,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        raise GoogleAuthError("Invalid or expired Google credential.") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("Invalid Google credential.")
    email = (claims.get("email") or "").strip()
    sub = claims.get("sub")
    if not sub or not email:
        raise GoogleAuthError("Google did not return an email address.")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("Your Google email address is not verified.")
    return GoogleIdentity(sub=str(sub), email=email, name=claims.get("name"), picture=claims.get("picture"))
