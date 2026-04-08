from dataclasses import dataclass
import os

from .version import VERSION


@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv("INCUBATOR_DB_URL", "sqlite:///./incubator.db")
    serial_port: str = os.getenv("INCUBATOR_SERIAL_PORT", "/dev/ttyUSB0")
    serial_baud: int = int(os.getenv("INCUBATOR_SERIAL_BAUD", "115200"))
    serial_timeout: float = float(os.getenv("INCUBATOR_SERIAL_TIMEOUT", "1.0"))
    require_login: bool = os.getenv("INCUBATOR_REQUIRE_LOGIN", "false").lower() == "true"
    session_cookie_name: str = os.getenv("INCUBATOR_SESSION_COOKIE_NAME", "incubator_session")
    app_version: str = os.getenv("INCUBATOR_APP_VERSION", VERSION)
    setup_button_pin: int = int(os.getenv("INCUBATOR_SETUP_BUTTON_PIN", "2"))
    setup_button_hold_seconds: float = float(os.getenv("INCUBATOR_SETUP_BUTTON_HOLD_SECONDS", "4.0"))
    setup_hotspot_ssid: str = os.getenv("INCUBATOR_SETUP_AP_SSID", "Incubator-Setup")
    setup_hotspot_password: str = os.getenv("INCUBATOR_SETUP_AP_PASSWORD", "Incubator123")


settings = Settings()
