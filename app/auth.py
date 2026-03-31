import hashlib
import hmac
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Session as AuthSession


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    salt_hex, digest_hex = password_hash.split(":", maxsplit=1)
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 390000)
    return hmac.compare_digest(actual, expected)


def get_user_id_from_session(db: Session, session_token: str | None) -> int | None:
    """Resolve a session cookie token to a user id.

    Temporary-safe behavior for current migration phase:
    - Returns None for missing/invalid/expired tokens (no exceptions).
    - Uses hashed token lookup against `sessions.token_hash`.
    - Keeps web routes bootable even before full auth flow is finalized.
    """

    if not session_token:
        return None

    token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    stmt = select(AuthSession).where(AuthSession.token_hash == token_hash, AuthSession.expires_at > now).limit(1)
    active_session = db.scalar(stmt)
    if not active_session:
        return None
    return int(active_session.user_id)
