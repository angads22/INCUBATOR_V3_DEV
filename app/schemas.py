from pydantic import BaseModel, Field


class SetupStatus(BaseModel):
    device_id: str
    claimed: bool


class OnboardingPayload(BaseModel):
    pairing_code: str = Field(min_length=4)
    username: str = Field(min_length=3)
    email: str
    password: str = Field(min_length=8)
    device_name: str = Field(min_length=2)
    farm_name: str | None = None
    wifi_ssid: str | None = None
    wifi_password: str | None = None


class HotspotSetupPayload(BaseModel):
    ssid: str = ""
    wifi_password: str = ""
    device_name: str = Field(default="My Incubator", min_length=1)
    create_account: bool = False
    username: str | None = None
    email: str | None = None
    password: str | None = None


class HardwareCommand(BaseModel):
    action: str
    value: str | int | float | None = None
