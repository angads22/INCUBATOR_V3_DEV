from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv("INCUBATOR_DB_URL", "sqlite:///./incubator.db")
    serial_port: str = os.getenv("INCUBATOR_SERIAL_PORT", "/dev/ttyUSB0")
    serial_baud: int = int(os.getenv("INCUBATOR_SERIAL_BAUD", "115200"))
    serial_timeout: float = float(os.getenv("INCUBATOR_SERIAL_TIMEOUT", "1.0"))
    session_cookie_name: str = os.getenv("INCUBATOR_SESSION_COOKIE", "incubator_session")
    session_hours: int = int(os.getenv("INCUBATOR_SESSION_HOURS", "24"))
    session_secure_cookie: bool = os.getenv("INCUBATOR_SESSION_SECURE", "true").lower() == "true"


settings = Settings()
