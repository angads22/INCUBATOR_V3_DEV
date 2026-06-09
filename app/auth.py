import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Session as AuthSession, User

# Default session lifetime — 7 days.
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        salt_hex, digest_hex = password_hash.split(":", maxsplit=1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390000)
    return hmac.compare_digest(actual, expected)


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Return the matching user when credentials are valid, else None.

    Accepts either the username or the email address as the identifier.
    """
    if not username or not password:
        return None
    identifier = username.strip()
    user = db.scalar(
        select(User).where((User.username == identifier) | (User.email == identifier)).limit(1)
    )
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def create_session(db: Session, user_id: int, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS) -> str:
    """Create a server-side session and return the raw token for the cookie.

    Only the SHA-256 hash of the token is stored, so a database leak does not
    expose usable session cookies.
    """
    token = secrets.token_urlsafe(32)
    expires_at = _utcnow() + timedelta(seconds=ttl_seconds)
    db.add(AuthSession(user_id=user_id, token_hash=_token_hash(token), expires_at=expires_at))
    db.commit()
    return token


def destroy_session(db: Session, session_token: str | None) -> None:
    """Delete the session row backing a cookie token (idempotent)."""
    if not session_token:
        return
    token_hash = _token_hash(session_token)
    for row in db.scalars(select(AuthSession).where(AuthSession.token_hash == token_hash)).all():
        db.delete(row)
    db.commit()


def destroy_user_sessions(db: Session, user_id: int) -> None:
    """Invalidate every session for a user (e.g. after a password reset)."""
    for row in db.scalars(select(AuthSession).where(AuthSession.user_id == user_id)).all():
        db.delete(row)
    db.commit()


def has_any_user(db: Session) -> bool:
    """True once at least one account exists (used to auto-enforce login)."""
    return db.scalar(select(User.id).limit(1)) is not None


def get_user_id_from_session(db: Session, session_token: str | None) -> int | None:
    """Resolve a session cookie token to a user id.

    Temporary-safe behavior for current migration phase:
    - Returns None for missing/invalid/expired tokens (no exceptions).
    - Uses hashed token lookup against `sessions.token_hash`.
    - Keeps web routes bootable even before full auth flow is finalized.
    """

    if not session_token:
        return None

    token_hash = _token_hash(session_token)
    now = _utcnow()

    stmt = select(AuthSession).where(AuthSession.token_hash == token_hash, AuthSession.expires_at > now).limit(1)
    active_session = db.scalar(stmt)
    if not active_session:
        return None
    return int(active_session.user_id)
