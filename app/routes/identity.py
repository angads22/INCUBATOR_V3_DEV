"""Sign in with Google / Apple + linked-accounts API.

Flows (see services/identity_service.py for why a LAN appliance needs these):
  * Google: device-code flow — begin returns a short code + URL for the
    operator's phone; the UI polls until Google confirms.
  * Apple (or Google) via HTTPS broker: the operator pastes the short-lived
    signed assertion the broker page gave them.

Intents:
  * ``login``: succeeds only for identities already linked to a local account
    (or creates the owner on a device with no accounts at all).
  * ``link``: requires an authenticated session; attaches the identity to it.
"""

from __future__ import annotations

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import create_session, get_user_id_from_session
from ..config import settings
from ..database import get_db
from ..models import ActionLog
from ..services.identity_service import IdentityService

router = APIRouter()

identity_service = IdentityService(
    google_client_id=settings.google_oauth_client_id,
    google_client_secret=settings.google_oauth_client_secret,
    broker_url=settings.identity_broker_url,
    broker_secret=settings.identity_broker_secret,
)


class PollPayload(BaseModel):
    flow_id: str
    intent: str = "login"          # login | link


class BrokerPayload(BaseModel):
    assertion: str
    intent: str = "login"


def _session_user(db: Session, session_token: str | None) -> int | None:
    return get_user_id_from_session(db, session_token)


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_secure,
        path="/",
    )


def _finish(
    db: Session,
    response: Response,
    identity: dict,
    intent: str,
    user_id: int | None,
) -> dict:
    """Shared tail for both verification paths: log in or link."""
    provider, sub, email = identity["provider"], identity["sub"], identity.get("email")
    if intent == "link":
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in first to link an account")
        result = identity_service.link_identity(db, user_id, provider, sub, email)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("error", "link_failed"))
        db.add(ActionLog(action="identity.linked", payload=f"{provider}:{email or sub}"))
        db.commit()
        return {"ok": True, "linked": True, "provider": provider, "email": email}

    result = identity_service.login_with_identity(db, provider, sub, email)
    if not result.get("ok"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account isn't linked to the incubator yet. Sign in with your password, then link it in Settings.",
        )
    _set_cookie(response, create_session(db, result["user_id"], settings.session_ttl_seconds))
    db.add(ActionLog(
        action="identity.login",
        payload=f"{provider}:{email or sub}" + (" (owner created)" if result.get("created") else ""),
    ))
    db.commit()
    return {"ok": True, "username": result["username"], "created": result.get("created", False)}


@router.get("/api/identity/config")
def identity_config() -> dict:
    """Which provider buttons the UI should show."""
    return {"ok": True, **identity_service.config()}


@router.post("/api/identity/google/begin")
def google_begin() -> dict:
    result = identity_service.begin_google_device_flow()
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "google_unavailable"))
    return result


@router.post("/api/identity/google/poll")
def google_poll(
    payload: PollPayload,
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    result = identity_service.poll_google_device_flow(payload.flow_id)
    if result["status"] in ("pending", "expired"):
        return {"ok": True, "status": result["status"]}
    if result["status"] == "error":
        return {"ok": False, "status": "error", "error": result.get("error")}
    return {"status": "ok", **_finish(db, response, result, payload.intent, _session_user(db, session_token))}


@router.post("/api/identity/broker")
def broker_assertion(
    payload: BrokerPayload,
    response: Response,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    result = identity_service.verify_broker_assertion(payload.assertion)
    if result["status"] != "ok":
        raise HTTPException(status_code=400, detail=result.get("error", "invalid_assertion"))
    return {"status": "ok", **_finish(db, response, result, payload.intent, _session_user(db, session_token))}


@router.get("/api/identity/links")
def list_links(
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    user_id = _session_user(db, session_token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return {"ok": True, "links": identity_service.list_identities(db, user_id), **identity_service.config()}


@router.delete("/api/identity/links/{identity_id}")
def unlink(
    identity_id: int,
    db: Session = Depends(get_db),
    session_token: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> dict:
    user_id = _session_user(db, session_token)
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    result = identity_service.unlink_identity(db, user_id, identity_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="Linked account not found")
    db.add(ActionLog(action="identity.unlinked", payload=str(identity_id)))
    db.commit()
    return {"ok": True}
