from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    db_url: str = os.getenv("INCUBATOR_DB_URL", "sqlite:///./incubator.db")
    serial_port: str = os.getenv("INCUBATOR_SERIAL_PORT", "/dev/ttyUSB0")
    serial_baud: int = int(os.getenv("INCUBATOR_SERIAL_BAUD", "115200"))
    serial_timeout: float = float(os.getenv("INCUBATOR_SERIAL_TIMEOUT", "1.0"))


settings = Settings()
