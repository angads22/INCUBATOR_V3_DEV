from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="owner", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class DeviceConfig(Base):
    __tablename__ = "device_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    claimed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    claim_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    farm_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    wifi_ssid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class Incubator(Base):
    __tablename__ = "incubators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="idle", nullable=False)


class Egg(Base):
    __tablename__ = "eggs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incubator_id: Mapped[int] = mapped_column(ForeignKey("incubators.id"), nullable=False)
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    # Region-of-interest for per-egg crops from a full camera frame. When unset
    # the camera service returns the full frame. Pixel coordinates in the
    # full-resolution image.
    roi_x: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roi_y: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roi_w: Mapped[int | None] = mapped_column(Integer, nullable=True)
    roi_h: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SensorLog(Base):
    __tablename__ = "sensor_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incubator_id: Mapped[int] = mapped_column(ForeignKey("incubators.id"), nullable=False)
    temperature_c: Mapped[float] = mapped_column(Float, nullable=False)
    humidity_pct: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class ActionLog(Base):
    __tablename__ = "action_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class ModelResult(Base):
    __tablename__ = "model_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    egg_id: Mapped[int | None] = mapped_column(ForeignKey("eggs.id"), nullable=True)
    image_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_backend: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    predicted_label: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class StageTest(Base):
    """One incubation-stage test from the Testing tab.

    Persists predicted-vs-actual so effectiveness (MAE in days) accumulates
    across sessions. ``actual_day`` is null until the operator records ground
    truth for the image.
    """

    __tablename__ = "stage_tests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_path: Mapped[str] = mapped_column(Text, nullable=False)
    predicted_day: Mapped[float] = mapped_column(Float, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="unclear", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    backend: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class EggPhoto(Base):
    """A stored, labeled egg photo managed by the storage janitor.

    Each row owns a JPEG on disk under <captures_dir>/eggs. The janitor prunes
    the OLDEST non-pinned rows (and their files) when the SD card gets close to
    full, so labeled candling photos accumulate safely without ever wedging the
    appliance on a full disk. ``pinned`` photos are never auto-deleted.
    """

    __tablename__ = "egg_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    egg_id: Mapped[int | None] = mapped_column(ForeignKey("eggs.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(64), default="egg", nullable=False)
    path: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    backend: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class GrowthObservation(Base):
    """One vision reading of an egg over time, so development can be tracked.

    A timeline of these per egg lets the growth engine measure whether an embryo
    is advancing (day estimate climbing across candlings), stalled, non-viable,
    or ready to hatch — and drive incubator actions (lockdown, flagging) off it.
    """

    __tablename__ = "growth_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    egg_id: Mapped[int] = mapped_column(ForeignKey("eggs.id"), nullable=False)
    day_estimate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), default="unclear", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    # Classifier label (fertile / infertile / dead_embryo / ...), when available.
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    backend: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="candle", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
