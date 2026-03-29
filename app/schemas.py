from pydantic import BaseModel, EmailStr, Field


class SetupStatus(BaseModel):
    device_id: str
    claimed: bool


class OnboardingPayload(BaseModel):
    pairing_code: str = Field(min_length=4)
    username: str = Field(min_length=3)
    email: EmailStr
    password: str = Field(min_length=8)
    device_name: str = Field(min_length=2)
    farm_name: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None


class LoginPayload(BaseModel):
    username: str
    password: str


class SettingsPayload(BaseModel):
    target_temp_c: float
    target_humidity_pct: float
    heater_enabled: bool
    fan_enabled: bool
    turner_enabled: bool
    alarm_enabled: bool
    alarm_temp_delta_c: float = 1.0
    alarm_humidity_delta_pct: float = 8.0
    refresh_interval_sec: int = 5


class ControlPayload(BaseModel):
    enabled: bool


class HealthResponse(BaseModel):
    ok: bool
    service: str
    timestamp: str


class EnvironmentResponse(BaseModel):
    temperature_c: float
    humidity_pct: float
    target_temp_c: float
    target_humidity_pct: float
    stale: bool
    hardware_online: bool
    sensor_online: bool
    simulated_mode: bool
    captured_at: str


class StatusResponse(BaseModel):
    ok: bool
    state: str
    hardware_online: bool
    sensor_online: bool
    simulated_mode: bool
    heater: bool
    fan: bool
    turner: bool
    alarm_enabled: bool
    alarms: list[str]
    last_updated: str


class DeviceModeResponse(BaseModel):
    mode: str
    simulated_mode: bool
