"""Sign in with Google / Apple for a LAN appliance, plus account linking.

Why this looks different from a normal web app
----------------------------------------------
Standard OAuth needs a fixed, registrable redirect URL. This device lives at
``http://incubator-xxxx.local:8000`` — a different mDNS name per unit, plain
HTTP — which no provider will accept. The two honest paths that DO work:

* **Google — device authorization grant** (the "TV flow"): the device asks
  Google for a short user code, the operator types it at google.com/device on
  any phone/laptop, and the device polls Google's token endpoint until the
  grant completes. The ID token arrives here directly from Google over TLS, so
  per OIDC Core §3.1.3.7 the TLS channel authenticates the issuer and local
  claim checks (iss / aud / exp) complete validation — no JWKS crypto needed.

* **Apple — HTTPS broker assertion**: Apple requires HTTPS return URLs and
  registered domains, full stop. A broker page (the fleet's cloud, at
  IDENTITY_BROKER_URL) runs the real Sign-in-with-Apple leg, then gives the
  operator a short-lived code — an HMAC-signed assertion over the verified
  identity — to paste into the device. The device checks the HMAC with the
  shared fleet secret (stdlib, offline). The broker can relay Google too.

Security decisions
------------------
* A provider identity can only LOG IN if it is already linked to a local
  account (or if the device has no accounts at all, in which case the first
  provider sign-in becomes the owner). Without this, any Google account on
  Earth could log into a farmer's incubator.
* Linking requires an authenticated session; unlinking never strips the
  password (local login always survives).
* Device-flow state lives in memory with expiry — nothing secret is persisted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import LinkedIdentity, User

logger = logging.getLogger(__name__)

PROVIDERS = ("google", "apple")

_GOOGLE_DEVICE_CODE_URL = "https://oauth2.googleapis.com/device/code"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_GOOGLE_SCOPE = "openid email profile"

# Broker assertions are single-use and short-lived.
_ASSERTION_MAX_AGE_SECONDS = 600

_USERNAME_SAFE_RE = re.compile(r"[^a-z0-9_.-]+")


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _jwt_claims_unverified(id_token: str) -> dict[str, Any]:
    """Decode JWT claims WITHOUT signature verification.

    Only ever called on tokens received directly from Google's token endpoint
    over TLS (see module docstring) — never on tokens supplied by a browser.
    """
    try:
        payload = id_token.split(".")[1]
        return json.loads(_b64url_decode(payload))
    except (IndexError, ValueError, binascii.Error) as exc:
        raise ValueError(f"malformed id_token: {exc}") from exc


class IdentityService:
    def __init__(
        self,
        google_client_id: str = "",
        google_client_secret: str = "",
        broker_url: str = "",
        broker_secret: str = "",
    ) -> None:
        self.google_client_id = google_client_id
        self.google_client_secret = google_client_secret
        self.broker_url = broker_url
        self.broker_secret = broker_secret
        self._lock = threading.Lock()
        # flow_id -> {device_code, interval, expires_at, last_poll}
        self._flows: dict[str, dict[str, Any]] = {}
        # Replay guard for broker assertions (sig -> expiry) — in-memory is
        # fine: assertions expire in minutes and a reboot empties sessions too.
        self._seen_assertions: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Capability discovery (drives which buttons the UI shows)
    # ------------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        return {
            "google": bool(self.google_client_id),
            "apple": bool(self.broker_secret),
            "broker_url": self.broker_url,
        }

    # ------------------------------------------------------------------
    # Google device flow
    # ------------------------------------------------------------------

    def begin_google_device_flow(self) -> dict[str, Any]:
        """Ask Google for a user code; returns what the UI must display."""
        if not self.google_client_id:
            return {"ok": False, "error": "google_not_configured"}
        try:
            import httpx

            resp = httpx.post(
                _GOOGLE_DEVICE_CODE_URL,
                data={"client_id": self.google_client_id, "scope": _GOOGLE_SCOPE},
                timeout=10.0,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — offline / DNS / Google down
            logger.warning("Google device-code request failed: %s", exc)
            return {"ok": False, "error": "google_unreachable"}
        if "device_code" not in data:
            logger.warning("Google device-code error: %s", data.get("error", data))
            return {"ok": False, "error": data.get("error", "google_error")}

        flow_id = secrets.token_urlsafe(16)
        with self._lock:
            self._prune_flows()
            self._flows[flow_id] = {
                "device_code": data["device_code"],
                "interval": max(3, int(data.get("interval", 5))),
                "expires_at": time.time() + int(data.get("expires_in", 1800)),
                "last_poll": 0.0,
            }
        return {
            "ok": True,
            "flow_id": flow_id,
            "user_code": data.get("user_code"),
            "verification_url": data.get("verification_url") or data.get("verification_uri") or "https://google.com/device",
            "expires_in": int(data.get("expires_in", 1800)),
            "interval": max(3, int(data.get("interval", 5))),
        }

    def poll_google_device_flow(self, flow_id: str) -> dict[str, Any]:
        """One poll against Google's token endpoint for a pending flow.

        Returns {"status": "pending"|"expired"|"error"} or
        {"status": "ok", "sub": ..., "email": ...} once the operator approved.
        """
        with self._lock:
            flow = self._flows.get(flow_id)
            if not flow:
                return {"status": "error", "error": "unknown_flow"}
            if time.time() > flow["expires_at"]:
                self._flows.pop(flow_id, None)
                return {"status": "expired"}
            # Respect Google's minimum poll interval regardless of UI timing.
            if time.time() - flow["last_poll"] < flow["interval"]:
                return {"status": "pending"}
            flow["last_poll"] = time.time()
            device_code = flow["device_code"]

        try:
            import httpx

            resp = httpx.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.google_client_id,
                    "client_secret": self.google_client_secret,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                timeout=10.0,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Google device-token poll failed: %s", exc)
            return {"status": "pending"}  # transient — keep the flow alive

        error = data.get("error")
        if error in ("authorization_pending", "slow_down"):
            if error == "slow_down":
                with self._lock:
                    if flow_id in self._flows:
                        self._flows[flow_id]["interval"] += 5
            return {"status": "pending"}
        if error:
            with self._lock:
                self._flows.pop(flow_id, None)
            return {"status": "error", "error": error}

        id_token = data.get("id_token", "")
        try:
            claims = _jwt_claims_unverified(id_token)
        except ValueError as exc:
            logger.warning("Google id_token parse failed: %s", exc)
            return {"status": "error", "error": "bad_id_token"}
        # TLS to Google authenticated the channel; finish OIDC validation.
        if claims.get("iss") not in _GOOGLE_ISSUERS:
            return {"status": "error", "error": "bad_issuer"}
        if claims.get("aud") != self.google_client_id:
            return {"status": "error", "error": "bad_audience"}
        if float(claims.get("exp", 0)) < time.time():
            return {"status": "error", "error": "token_expired"}
        with self._lock:
            self._flows.pop(flow_id, None)
        return {
            "status": "ok",
            "provider": "google",
            "sub": str(claims.get("sub", "")),
            "email": claims.get("email"),
        }

    def _prune_flows(self) -> None:
        now = time.time()
        for fid in [f for f, v in self._flows.items() if now > v["expires_at"]]:
            self._flows.pop(fid, None)
        for sig in [s for s, exp in self._seen_assertions.items() if now > exp]:
            self._seen_assertions.pop(sig, None)

    # ------------------------------------------------------------------
    # Broker assertions (Apple, or Google-via-broker)
    # ------------------------------------------------------------------

    def make_broker_assertion(self, provider: str, sub: str, email: str | None, *, iat: float | None = None) -> str:
        """Create an assertion the way the HTTPS broker does (used by tests /
        as the reference implementation for the broker page)."""
        payload = json.dumps({
            "provider": provider, "sub": sub, "email": email,
            "iat": iat if iat is not None else time.time(),
            "nonce": secrets.token_hex(8),
        }).encode()
        body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
        sig = hmac.new(self.broker_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def verify_broker_assertion(self, assertion: str) -> dict[str, Any]:
        """Verify an operator-pasted broker assertion. Constant-time HMAC,
        expiry window, single-use replay guard."""
        if not self.broker_secret:
            return {"status": "error", "error": "broker_not_configured"}
        try:
            body, sig = assertion.strip().split(".", 1)
            expected = hmac.new(self.broker_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return {"status": "error", "error": "bad_signature"}
            claims = json.loads(_b64url_decode(body))
        except (ValueError, binascii.Error):
            return {"status": "error", "error": "malformed_assertion"}
        provider = claims.get("provider")
        if provider not in PROVIDERS:
            return {"status": "error", "error": "bad_provider"}
        iat = float(claims.get("iat", 0))
        if not (time.time() - _ASSERTION_MAX_AGE_SECONDS <= iat <= time.time() + 60):
            return {"status": "error", "error": "assertion_expired"}
        sub = str(claims.get("sub") or "")
        if not sub:
            return {"status": "error", "error": "missing_subject"}
        with self._lock:
            self._prune_flows()
            if sig in self._seen_assertions:
                return {"status": "error", "error": "assertion_replayed"}
            self._seen_assertions[sig] = iat + _ASSERTION_MAX_AGE_SECONDS
        return {"status": "ok", "provider": provider, "sub": sub, "email": claims.get("email")}

    # ------------------------------------------------------------------
    # Login / link / unlink against the local user table
    # ------------------------------------------------------------------

    def login_with_identity(self, db: Session, provider: str, sub: str, email: str | None) -> dict[str, Any]:
        """Resolve a verified provider identity to a local user.

        * identity already linked → that user logs in;
        * no local accounts exist at all → first sign-in becomes the owner;
        * accounts exist but identity unlinked → REFUSED (link it first, from
          a logged-in session) so a stranger's Google account can never claim
          someone's incubator.
        """
        row = db.scalar(
            select(LinkedIdentity).where(
                LinkedIdentity.provider == provider, LinkedIdentity.subject == sub
            )
        )
        if row:
            user = db.get(User, row.user_id)
            if user:
                return {"ok": True, "user_id": user.id, "username": user.username, "created": False}
            db.delete(row)  # orphaned link — clean up and fall through
            db.commit()

        if db.scalar(select(User.id).limit(1)) is not None:
            return {"ok": False, "error": "identity_not_linked"}

        # First boot, no accounts: this provider identity becomes the owner.
        username = self._username_from_email(db, email, provider)
        user = User(
            username=username,
            email=email or f"{username}@{provider}.invalid",
            # Random unguessable password hash — provider login is the key;
            # the operator can set a real password from Settings.
            password_hash=self._random_password_hash(),
            role="owner",
        )
        db.add(user)
        db.flush()
        db.add(LinkedIdentity(user_id=user.id, provider=provider, subject=sub, email=email))
        db.commit()
        logger.info("Created owner account '%s' from %s sign-in", username, provider)
        return {"ok": True, "user_id": user.id, "username": user.username, "created": True}

    def link_identity(self, db: Session, user_id: int, provider: str, sub: str, email: str | None) -> dict[str, Any]:
        row = db.scalar(
            select(LinkedIdentity).where(
                LinkedIdentity.provider == provider, LinkedIdentity.subject == sub
            )
        )
        if row:
            if row.user_id == user_id:
                row.email = email or row.email
                db.commit()
                return {"ok": True, "already_linked": True}
            return {"ok": False, "error": "linked_to_other_account"}
        db.add(LinkedIdentity(user_id=user_id, provider=provider, subject=sub, email=email))
        db.commit()
        return {"ok": True, "already_linked": False}

    def unlink_identity(self, db: Session, user_id: int, identity_id: int) -> dict[str, Any]:
        row = db.get(LinkedIdentity, identity_id)
        if not row or row.user_id != user_id:
            return {"ok": False, "error": "not_found"}
        db.delete(row)
        db.commit()
        return {"ok": True}

    def list_identities(self, db: Session, user_id: int) -> list[dict[str, Any]]:
        rows = db.scalars(
            select(LinkedIdentity).where(LinkedIdentity.user_id == user_id).order_by(LinkedIdentity.id)
        ).all()
        return [
            {"id": r.id, "provider": r.provider, "email": r.email,
             "linked_at": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ")}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _random_password_hash() -> str:
        from ..auth import hash_password

        return hash_password(secrets.token_urlsafe(32))

    @staticmethod
    def _username_from_email(db: Session, email: str | None, provider: str) -> str:
        base = (email or "").split("@")[0].lower()
        base = _USERNAME_SAFE_RE.sub("", base) or f"{provider}-user"
        candidate, n = base, 1
        while db.scalar(select(User.id).where(User.username == candidate).limit(1)) is not None:
            n += 1
            candidate = f"{base}{n}"
        return candidate
