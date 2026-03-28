import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Session as UserSession


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


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(db: Session, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.utcnow() + timedelta(hours=settings.session_hours)
    db.add(UserSession(user_id=user_id, token_hash=_hash_token(token), expires_at=expires_at))
    db.commit()
    return token


def get_user_id_from_session(db: Session, token: str | None) -> int | None:
    if not token:
        return None
    token_hash = _hash_token(token)
    row = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if not row:
        return None
    if row.expires_at < datetime.utcnow():
        db.delete(row)
        db.commit()
        return None
    return row.user_id


def clear_session(db: Session, token: str | None) -> None:
    if not token:
        return
    token_hash = _hash_token(token)
    row = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash))
    if row:
        db.delete(row)
        db.commit()
