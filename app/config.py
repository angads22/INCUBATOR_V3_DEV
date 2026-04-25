from dataclasses import dataclass, field
import os

from .version import VERSION


@dataclass(frozen=True)
class Settings:
    # --- Core ---
    db_url: str = field(default_factory=lambda: os.getenv("INCUBATOR_DB_URL", "sqlite:///./database/incubator.db"))
    app_version: str = field(default_factory=lambda: os.getenv("INCUBATOR_APP_VERSION", VERSION))

    # --- Auth ---
    require_login: bool = field(default_factory=lambda: os.getenv("INCUBATOR_REQUIRE_LOGIN", "false").lower() == "true")
    session_cookie_name: str = field(default_factory=lambda: os.getenv("INCUBATOR_SESSION_COOKIE_NAME", "incubator_session"))

    # --- GPIO pin assignments (BCM numbering) ---
    # Sensors
    gpio_dht_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_DHT_PIN", "4")))        # DHT22 data pin
    # Outputs (relays are active-LOW by default)
    gpio_heater_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_HEATER_PIN", "17")))  # Heater relay
    gpio_fan_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_FAN_PIN", "27")))        # Fan relay
    gpio_turner_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_TURNER_PIN", "22")))  # Turner motor step
    gpio_turner_dir_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_TURNER_DIR_PIN", "23")))  # Turner direction
    gpio_candle_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_CANDLE_PIN", "24"))) # Candle LED
    gpio_alarm_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_ALARM_PIN", "25")))   # Buzzer
    gpio_lock_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_LOCK_PIN", "12")))     # Lock relay
    gpio_door_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_DOOR_PIN", "13")))     # Door servo signal
    # Inputs
    gpio_setup_button_pin: int = field(default_factory=lambda: int(os.getenv("GPIO_SETUP_BUTTON_PIN", "18")))  # Onboarding button
    gpio_relay_active_low: bool = field(default_factory=lambda: os.getenv("GPIO_RELAY_ACTIVE_LOW", "true").lower() == "true")

    # --- Setup / Onboarding ---
    setup_button_hold_seconds: float = field(default_factory=lambda: float(os.getenv("INCUBATOR_SETUP_BUTTON_HOLD_SECONDS", "4.0")))
    ap_ssid_prefix: str = field(default_factory=lambda: os.getenv("INCUBATOR_AP_SSID_PREFIX", "Incubator"))
    ap_password: str = field(default_factory=lambda: os.getenv("INCUBATOR_AP_PASSWORD", "setup1234"))
    ap_ip: str = field(default_factory=lambda: os.getenv("INCUBATOR_AP_IP", "10.42.0.1"))
    # Auto-start hotspot when device has no WiFi config on boot
    auto_hotspot_on_unclaimed: bool = field(default_factory=lambda: os.getenv("INCUBATOR_AUTO_HOTSPOT", "true").lower() == "true")

    # --- Camera ---
    camera_backend: str = field(default_factory=lambda: os.getenv("CAMERA_BACKEND", "picamera2"))  # picamera2 | opencv | mock
    camera_image_dir: str = field(default_factory=lambda: os.getenv("CAMERA_IMAGE_DIR", "./captures"))
    camera_resolution_w: int = field(default_factory=lambda: int(os.getenv("CAMERA_RES_W", "1920")))
    camera_resolution_h: int = field(default_factory=lambda: int(os.getenv("CAMERA_RES_H", "1080")))

    # --- Vision model ---
    # Backend: 'tflite' for local on-device inference, 'api' for remote, 'mock' for dev
    vision_backend: str = field(default_factory=lambda: os.getenv("VISION_BACKEND", "mock"))
    vision_tflite_model_path: str = field(default_factory=lambda: os.getenv("VISION_TFLITE_MODEL", "./models/vision/model.tflite"))
    vision_api_url: str = field(default_factory=lambda: os.getenv("VISION_API_URL", "").strip())
    vision_api_key: str = field(default_factory=lambda: os.getenv("VISION_API_KEY", "").strip())
    vision_confidence_threshold: float = field(default_factory=lambda: float(os.getenv("VISION_CONFIDENCE_THRESHOLD", "0.65")))

    # --- Sensor polling ---
    sensor_poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("SENSOR_POLL_INTERVAL", "30")))
    sensor_log_to_db: bool = field(default_factory=lambda: os.getenv("SENSOR_LOG_TO_DB", "true").lower() == "true")

    # --- Cloud (optional) ---
    enable_cloud_sync: bool = field(default_factory=lambda: os.getenv("ENABLE_CLOUD_SYNC", "false").lower() == "true")
    domain_api_base: str = field(default_factory=lambda: os.getenv("DOMAIN_API_BASE", "").strip())
    device_shared_secret: str = field(default_factory=lambda: os.getenv("DEVICE_SHARED_SECRET", "").strip())
    heartbeat_interval_seconds: int = field(default_factory=lambda: int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300")))

    # --- Dev overrides ---
    gpio_mock: bool = field(default_factory=lambda: os.getenv("GPIO_MOCK", "false").lower() == "true")
    button_mock_file: str = field(default_factory=lambda: os.getenv("INCUBATOR_BUTTON_MOCK_FILE", ""))


settings = Settings()
