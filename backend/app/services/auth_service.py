"""
Authentication service — password hashing and JWT token management.

Uses bcrypt directly for password hashing (passlib 1.7.4 is incompatible with
bcrypt >= 4.0 due to an unfixed upstream bug) and python-jose for JWT.
"""

import bcrypt as _bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30


def hash_password(password: str) -> str:
    """
    Hash a plain-text password with bcrypt.

    Args:
        password: Plain-text password from the registration form.

    Returns:
        bcrypt hash string safe to store in the database.
    """
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plain-text password against a stored bcrypt hash.

    Args:
        plain_password:  Password submitted by the user.
        hashed_password: bcrypt hash from the database.

    Returns:
        True if the password matches, False otherwise.
    """
    return _bcrypt.checkpw(plain_password.encode(), hashed_password.encode())


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """
    Create a signed JWT access token.

    Args:
        data:          Payload dict; must include a 'sub' key (client email).
        expires_delta: Token lifetime. Defaults to ACCESS_TOKEN_EXPIRE_DAYS (30 days).

    Returns:
        Encoded JWT string.
    """
    settings = get_settings()
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    )
    to_encode["exp"] = expire
    return jwt.encode(to_encode, settings.secret_key, algorithm=ALGORITHM)


async def get_current_client(token: str, db: AsyncSession):
    """
    Decode a JWT token and return the corresponding Client from the DB.

    Args:
        token: Raw JWT string (without 'Bearer ' prefix).
        db:    Active async DB session.

    Returns:
        Client instance if the token is valid and account is active.

    Raises:
        ValueError: If the token is invalid, expired, or the client does not exist.
    """
    from app.models.client import Client

    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise ValueError("Token missing subject.")
    except JWTError as exc:
        raise ValueError(f"Invalid token: {exc}") from exc

    result = await db.execute(select(Client).where(Client.email == email))
    client = result.scalar_one_or_none()
    if client is None or not client.is_active:
        raise ValueError("Client not found or inactive.")
    return client
