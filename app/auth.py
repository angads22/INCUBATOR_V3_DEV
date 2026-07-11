import hashlib
import hmac
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Session as AuthSession, User

# Default session lifetime — 7 days.
DEFAULT_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

# ── Login throttling (brute-force protection) ─────────────────────────────
# After LOGIN_MAX_FAILURES consecutive failures for the same client+identifier
# pair, further attempts are refused for LOGIN_LOCKOUT_SECONDS. In-memory on
# purpose: it protects the running process, resets on reboot (which is fine —
# a reboot takes longer than the lockout), and adds zero SD-card writes.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 60.0

_throttle_lock = threading.Lock()
_failed_logins: dict[str, tuple[int, float]] = {}  # key -> (count, last_failure_ts)


def _throttle_key(client_ip: str, identifier: str) -> str:
    return f"{client_ip}|{(identifier or '').strip().lower()}"


def login_throttled(client_ip: str, identifier: str) -> float:
    """Seconds the caller must still wait, or 0.0 when the attempt may proceed."""
    with _throttle_lock:
        entry = _failed_logins.get(_throttle_key(client_ip, identifier))
        if not entry:
            return 0.0
        count, last_ts = entry
        if count < LOGIN_MAX_FAILURES:
            return 0.0
        remaining = LOGIN_LOCKOUT_SECONDS - (time.time() - last_ts)
        return max(0.0, remaining)


def record_login_result(client_ip: str, identifier: str, success: bool) -> None:
    key = _throttle_key(client_ip, identifier)
    with _throttle_lock:
        if success:
            _failed_logins.pop(key, None)
            return
        count, _ = _failed_logins.get(key, (0, 0.0))
        _failed_logins[key] = (count + 1, time.time())
        # Opportunistic prune so the map can't grow unbounded under a spray.
        if len(_failed_logins) > 1000:
            cutoff = time.time() - LOGIN_LOCKOUT_SECONDS
            for k in [k for k, (_, ts) in _failed_logins.items() if ts < cutoff]:
                _failed_logins.pop(k, None)


def reset_login_throttle() -> None:
    """Test helper — drop all throttle state."""
    with _throttle_lock:
        _failed_logins.clear()


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
    # Opportunistic hygiene: purge expired sessions whenever a new one is made,
    # so stale token hashes don't accumulate forever on the SD card.
    for row in db.scalars(select(AuthSession).where(AuthSession.expires_at <= _utcnow())).all():
        db.delete(row)
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
