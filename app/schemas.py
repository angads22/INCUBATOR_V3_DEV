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


class HardwareCommand(BaseModel):
    action: str
    value: str | int | float | None = None


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


class ControlPayload(BaseModel):
    enabled: bool
