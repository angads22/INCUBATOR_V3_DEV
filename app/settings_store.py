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
    "alarm_temp_delta_c": "1.0",
    "alarm_humidity_delta_pct": "8.0",
    "refresh_interval_sec": "5",
    "simulation_noise": "normal",
    "private_access_hint": "vpn_or_reverse_proxy",
}


def ensure_defaults(db: Session) -> None:
    changed = False
    for key, value in DEFAULT_SETTINGS.items():
        row = db.scalar(select(AppSetting).where(AppSetting.key == key))
        if not row:
            db.add(AppSetting(key=key, value=value))
            changed = True
    if changed:
        db.commit()


def get_settings(db: Session) -> dict[str, str]:
    rows = db.scalars(select(AppSetting)).all()
    if not rows:
        ensure_defaults(db)
        rows = db.scalars(select(AppSetting)).all()
    return {row.key: row.value for row in rows}


def update_settings(db: Session, updates: dict[str, str]) -> dict[str, str]:
    for key, value in updates.items():
        row = db.scalar(select(AppSetting).where(AppSetting.key == key))
        if row:
            row.value = value
        else:
            db.add(AppSetting(key=key, value=value))
    db.commit()
    return get_settings(db)
