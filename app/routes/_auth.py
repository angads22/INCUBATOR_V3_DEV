"""Shared auth gate for every API router.

The appliance lifecycle: a fresh device is open so onboarding can run; as soon
as an owner account exists (or ``INCUBATOR_REQUIRE_LOGIN`` is set) every page
and control API requires a valid session. Each router used to carry its own
copy of this rule — import it from here instead so no endpoint is forgotten.
"""

from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import get_user_id_from_session, has_any_user
from ..config import settings


def login_required(db: Session) -> bool:
    """Auth is enforced when explicitly required, or once an account exists."""
    return settings.require_login or has_any_user(db)


def session_user_id(db: Session, session_token: str | None) -> int | None:
    return get_user_id_from_session(db, session_token)


def require_api_user(db: Session, session_token: str | None) -> None:
    """Raise 401 for APIs when login is required and no valid session is present."""
    if login_required(db) and not get_user_id_from_session(db, session_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def auth_redirect(db: Session, session_token: str | None):
    """For HTML pages: a redirect to /login when a session is required but absent, else None."""
    if login_required(db) and not get_user_id_from_session(db, session_token):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return None
