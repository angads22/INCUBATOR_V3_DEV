from dataclasses import dataclass, field
import os

from .version import VERSION


@dataclass(frozen=True)
class Settings:
    # --- Core ---
    db_url: str = field(default_factory=lambda: os.getenv("INCUBATOR_DB_URL", "sqlite:///./database/incubator.db"))
    app_version: str = field(default_factory=lambda: os.getenv("INCUBATOR_APP_VERSION", VERSION))

    # --- Auth ---
    # When false, login is still auto-enforced once an owner account exists.
    require_login: bool = field(default_factory=lambda: os.getenv("INCUBATOR_REQUIRE_LOGIN", "false").lower() == "true")
    session_cookie_name: str = field(default_factory=lambda: os.getenv("INCUBATOR_SESSION_COOKIE_NAME", "incubator_session"))
    # Mark the session cookie Secure — enable when serving over HTTPS.
    session_secure: bool = field(default_factory=lambda: os.getenv("INCUBATOR_SESSION_SECURE", "false").lower() == "true")
    # Session lifetime in seconds (default 7 days).
    session_ttl_seconds: int = field(default_factory=lambda: int(os.getenv("INCUBATOR_SESSION_TTL", "604800")))

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
    # Blank = OPEN setup network (no Wi-Fi password). The operator just joins
    # "Incubator-XXXX", lands on the captive portal, and creates an account in
    # the wizard. Set a non-empty value here only if you want a WPA2 setup AP.
    ap_password: str = field(default_factory=lambda: os.getenv("INCUBATOR_AP_PASSWORD", ""))
    ap_ip: str = field(default_factory=lambda: os.getenv("INCUBATOR_AP_IP", "10.42.0.1"))
    # Auto-start hotspot when device has no WiFi config on boot
    auto_hotspot_on_unclaimed: bool = field(default_factory=lambda: os.getenv("INCUBATOR_AUTO_HOTSPOT", "true").lower() == "true")
    # Wi-Fi regulatory country (ISO 3166-1 alpha-2). On RPi OS Bookworm the WLAN
    # radio is rfkill soft-blocked until a country is set, so the hotspot never
    # comes up without this. Baked into the image by build_image.sh / init_pi.sh.
    wifi_country: str = field(default_factory=lambda: os.getenv("INCUBATOR_WIFI_COUNTRY", "US").strip().upper())

    # --- Camera ---
    camera_backend: str = field(default_factory=lambda: os.getenv("CAMERA_BACKEND", "picamera2"))  # picamera2 | opencv | mock
    camera_image_dir: str = field(default_factory=lambda: os.getenv("CAMERA_IMAGE_DIR", "./captures"))
    camera_resolution_w: int = field(default_factory=lambda: int(os.getenv("CAMERA_RES_W", "1920")))
    camera_resolution_h: int = field(default_factory=lambda: int(os.getenv("CAMERA_RES_H", "1080")))
    # Low-res preview stream (additive — used by the live-preview card + MJPEG endpoint).
    camera_preview_w: int = field(default_factory=lambda: int(os.getenv("CAMERA_PREVIEW_W", "640")))
    camera_preview_h: int = field(default_factory=lambda: int(os.getenv("CAMERA_PREVIEW_H", "480")))
    camera_preview_fps: float = field(default_factory=lambda: float(os.getenv("CAMERA_PREVIEW_FPS", "1.0")))
    # Live preview card + MJPEG stream are off by default so behaviour is unchanged.
    camera_stream_enabled: bool = field(default_factory=lambda: os.getenv("CAMERA_STREAM_ENABLED", "false").lower() == "true")
    # Transient preview frames live in tmpfs (RAM), never the SD card.
    camera_frame_dir: str = field(default_factory=lambda: os.getenv("CAMERA_FRAME_DIR", "/run/incubator/frames"))
    # Directory the Testing tab browses for saved captures. Defaults to the
    # capture dir so laptop dev works out of the box; the Pi env sets
    # /var/incubator/captures.
    captures_dir: str = field(default_factory=lambda: os.getenv("INCUBATOR_CAPTURES_DIR", os.getenv("CAMERA_IMAGE_DIR", "./captures")))

    # --- Vision model ---
    # Backend: 'auto' (plug-and-play: use a dropped-in model, else API, else mock),
    # 'tflite' (force on-device), 'api' (force remote), 'mock' (force dev).
    vision_backend: str = field(default_factory=lambda: os.getenv("VISION_BACKEND", "auto"))
    vision_tflite_model_path: str = field(default_factory=lambda: os.getenv("VISION_TFLITE_MODEL", "./models/vision/model.tflite"))
    vision_api_url: str = field(default_factory=lambda: os.getenv("VISION_API_URL", "").strip())
    vision_api_key: str = field(default_factory=lambda: os.getenv("VISION_API_KEY", "").strip())
    vision_confidence_threshold: float = field(default_factory=lambda: float(os.getenv("VISION_CONFIDENCE_THRESHOLD", "0.65")))

    # --- Vision: incubation-stage estimator (Testing tab) ---
    # 'heuristic' works today with no trained model; 'tflite' loads a dropped-in
    # model; 'mock' returns a fixed result for dev/CI.
    vision_stage_backend: str = field(default_factory=lambda: os.getenv("VISION_STAGE_BACKEND", "heuristic"))
    vision_stage_model_path: str = field(default_factory=lambda: os.getenv("VISION_STAGE_MODEL", "/var/incubator/models/vision/stage.tflite"))
    # Total incubation length for the configured species (chicken = 21 days).
    incubation_days: int = field(default_factory=lambda: int(os.getenv("INCUBATION_DAYS", "21")))

    # --- Egg-photo storage + auto-prune (protects the SD card) ---
    # Labeled egg photos are saved under <captures_dir>/eggs and tracked in the
    # egg_photos table. A janitor deletes the OLDEST non-pinned photos when the
    # SD card gets close to full, so the appliance never wedges on a full disk.
    capture_storage_enabled: bool = field(default_factory=lambda: os.getenv("CAPTURE_STORAGE_ENABLED", "true").lower() == "true")
    # Start pruning once free space on the captures filesystem drops below this.
    capture_min_free_mb: int = field(default_factory=lambda: int(os.getenv("CAPTURE_MIN_FREE_MB", "300")))
    # Prune oldest photos until at least this much is free again (hysteresis so
    # we don't delete one photo per capture right at the threshold).
    capture_target_free_mb: int = field(default_factory=lambda: int(os.getenv("CAPTURE_TARGET_FREE_MB", "600")))
    # Optional hard cap on the egg-photo directory itself (MB). 0 disables it and
    # leaves only the free-space trigger active.
    capture_max_dir_mb: int = field(default_factory=lambda: int(os.getenv("CAPTURE_MAX_DIR_MB", "1024")))
    # Always keep at least this many of the newest photos, even under pressure.
    capture_keep_min: int = field(default_factory=lambda: int(os.getenv("CAPTURE_KEEP_MIN", "12")))
    # Optional age cap (days). Photos older than this are pruned first. 0 disables.
    capture_retention_days: int = field(default_factory=lambda: int(os.getenv("CAPTURE_RETENTION_DAYS", "0")))

    # --- Sensor polling ---
    sensor_poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("SENSOR_POLL_INTERVAL", "30")))
    sensor_log_to_db: bool = field(default_factory=lambda: os.getenv("SENSOR_LOG_TO_DB", "true").lower() == "true")

    # --- Cloud / OTA (optional) ---
    enable_cloud_sync: bool = field(default_factory=lambda: os.getenv("ENABLE_CLOUD_SYNC", "false").lower() == "true")
    domain_api_base: str = field(default_factory=lambda: os.getenv("DOMAIN_API_BASE", "").strip())
    device_shared_secret: str = field(default_factory=lambda: os.getenv("DEVICE_SHARED_SECRET", "").strip())
    heartbeat_interval_seconds: int = field(default_factory=lambda: int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "300")))
    # Unique device identifier written by firstboot.sh and embedded in the env file
    device_id: str = field(default_factory=lambda: os.getenv("INCUBATOR_DEVICE_ID", "").strip())
    # How often (seconds) the OTA timer fires; 0 = disabled (managed by systemd timer)
    ota_poll_interval_seconds: int = field(default_factory=lambda: int(os.getenv("OTA_POLL_INTERVAL_SECONDS", "0")))

    # --- Fleet MQTT bus (Phase 1) ---
    # Each unit publishes telemetry under <base>/<device_id>/... and subscribes
    # to <base>/<device_id>/cmd. Disabled by default so a standalone unit and
    # the test suite never touch a broker.
    mqtt_enabled: bool = field(default_factory=lambda: os.getenv("MQTT_ENABLED", "false").lower() == "true")
    mqtt_host: str = field(default_factory=lambda: os.getenv("MQTT_HOST", "").strip())
    mqtt_port: int = field(default_factory=lambda: int(os.getenv("MQTT_PORT", "1883")))
    mqtt_username: str = field(default_factory=lambda: os.getenv("MQTT_USERNAME", "").strip())
    mqtt_password: str = field(default_factory=lambda: os.getenv("MQTT_PASSWORD", "").strip())
    mqtt_base_topic: str = field(default_factory=lambda: os.getenv("MQTT_BASE_TOPIC", "fleet").strip().strip("/"))
    mqtt_telemetry_interval_seconds: int = field(default_factory=lambda: int(os.getenv("MQTT_TELEMETRY_INTERVAL", "30")))

    # --- Control daemon (Phase 3) ---
    # The safety-critical control loop runs as its OWN always-on process
    # (incubator-control.service) so an app/UI update — which restarts only
    # incubator.service — never pauses heater/turn control. Disabled by default
    # so the single-process app + tests behave exactly as before.
    control_daemon_enabled: bool = field(default_factory=lambda: os.getenv("CONTROL_DAEMON_ENABLED", "false").lower() == "true")
    control_interval_seconds: int = field(default_factory=lambda: int(os.getenv("CONTROL_INTERVAL", "10")))
    control_hysteresis_c: float = field(default_factory=lambda: float(os.getenv("CONTROL_HYSTERESIS_C", "0.4")))
    # Auto egg-turn cadence (hours); 0 disables scheduled turning.
    turn_interval_hours: float = field(default_factory=lambda: float(os.getenv("TURN_INTERVAL_HOURS", "3")))
    # Humidity actuation: off (monitor only) | fan (vent to lower RH).
    humidity_control_mode: str = field(default_factory=lambda: os.getenv("HUMIDITY_CONTROL_MODE", "off").strip().lower())
    control_state_path: str = field(default_factory=lambda: os.getenv("CONTROL_STATE_PATH", "/run/incubator/control-state.json"))
    control_command_path: str = field(default_factory=lambda: os.getenv("CONTROL_COMMAND_PATH", "/run/incubator/control-commands.jsonl"))

    # --- Dev overrides ---
    gpio_mock: bool = field(default_factory=lambda: os.getenv("GPIO_MOCK", "false").lower() == "true")
    button_mock_file: str = field(default_factory=lambda: os.getenv("INCUBATOR_BUTTON_MOCK_FILE", ""))


settings = Settings()
