from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AppSetting


DEFAULT_SETTINGS = {
    "target_temp_c": "37.5",
    "target_humidity_pct": "55",
    "heater_enabled": "false",
    "fan_enabled": "true",
    "turner_enabled": "true",
    "alarm_enabled": "true",
    # Alerting: readings outside target ± tolerance raise an alert; the
    # sensor counts as offline after this many consecutive failed reads.
    "alert_temp_tolerance_c": "1.0",
    "alert_humidity_tolerance_pct": "10",
    "alert_sensor_fail_count": "3",
    "private_access_hint": "vpn_or_reverse_proxy",
}


def ensure_defaults(db: Session) -> None:
    existing_keys = set(db.scalars(select(AppSetting.key)).all())
    for key, value in DEFAULT_SETTINGS.items():
        if key not in existing_keys:
            db.add(AppSetting(key=key, value=value))
    db.commit()


def get_settings(db: Session) -> dict[str, str]:
    rows = db.scalars(select(AppSetting)).all()
    if not rows:
        ensure_defaults(db)
        rows = db.scalars(select(AppSetting)).all()
    return {row.key: row.value for row in rows}


# Key under which the operator's optional setup-AP password lives. The setup
# network is OPEN until the operator deliberately sets one here (post-setup),
# so it never depends on a stale value baked into /etc/incubator.env.
AP_PASSWORD_KEY = "ap_setup_password"


def effective_ap_password(db: Session) -> str:
    """The setup-AP Wi-Fi password, or "" for an open network.

    Authoritative source is the DB (set by the operator after setup). We do NOT
    fall back to the INCUBATOR_AP_PASSWORD env var: that value is written once at
    first boot and would otherwise keep an OTA-updated unit locked with an old
    random key. No DB value → open network, every time.
    """
    row = db.scalar(select(AppSetting).where(AppSetting.key == AP_PASSWORD_KEY))
    return (row.value or "").strip() if row else ""


def set_ap_password(db: Session, password: str) -> None:
    """Set (or clear, when empty) the operator's setup-AP password."""
    update_settings(db, {AP_PASSWORD_KEY: (password or "").strip()})


def update_settings(db: Session, updates: dict[str, str]) -> dict[str, str]:
    if not updates:
        return get_settings(db)
    keys = list(updates.keys())
    existing = {row.key: row for row in db.scalars(select(AppSetting).where(AppSetting.key.in_(keys))).all()}
    for key, value in updates.items():
        if key in existing:
            existing[key].value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()
    return get_settings(db)
