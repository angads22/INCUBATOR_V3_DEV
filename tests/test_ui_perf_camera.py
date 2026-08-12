"""Button latency, camera enablement, and the header/settings reorganisation."""


def test_sensor_read_does_not_block_relay_writes():
    """The 'every button press takes forever' bug.

    A DHT22 read blocks for up to ~6 s (three attempts with retry sleeps).
    While it held the single shared GPIO lock, every relay command queued
    behind it. Reads and writes must use separate locks.
    """
    import threading
    import time

    from app import main

    svc = main.gpio_service   # the real wired service

    # Hold the SENSOR lock as a slow read would.
    started = threading.Event()

    def slow_read():
        with svc._sensor_lock:
            started.set()
            time.sleep(0.5)

    t = threading.Thread(target=slow_read, daemon=True)
    t.start()
    started.wait(1.0)

    # A relay write must not wait on the in-flight sensor read.
    t0 = time.time()
    acquired = svc._lock.acquire(timeout=0.2)
    elapsed = time.time() - t0
    if acquired:
        svc._lock.release()
    assert acquired, "relay write blocked behind a sensor read"
    assert elapsed < 0.2
    t.join(timeout=2)


def test_camera_enabled_in_firmware_and_gated():
    """Without camera_auto_detect the CSI camera is invisible to libcamera."""
    init = open("init_pi.sh").read()
    assert "camera_auto_detect=1" in init
    assert "dtoverlay=vc4-kms-v3d" in init
    assert "camera_auto_detect=1" in open("build_image.sh").read()


def test_camera_failures_explain_themselves():
    from app.services.camera_service import CameraService

    cam = CameraService(backend="mock")
    assert "python3-picamera2" in cam.failure_hint(ImportError("No module named picamera2"))
    assert "ribbon cable" in cam.failure_hint(RuntimeError("no cameras available"))
    # Mock backend reports healthy so dev boxes are not shown as broken.
    assert cam.diagnostics()["ok"] is True


def test_display_controls_live_in_settings_not_the_header():
    nav = open("app/templates/partials/nav.html").read()
    settings = open("app/templates/settings.html").read()
    assert "theme-dot" not in nav and "unitToggle" not in nav
    assert "theme-dot" in settings and 'id="unitToggle"' in settings


def test_no_em_dashes_in_user_facing_text():
    """The operator-visible surface stays free of em/en dashes."""
    import pathlib

    offenders = []
    for pat in ("app/templates/**/*.html", "app/static/js/*.js"):
        for p in pathlib.Path(".").glob(pat):
            text = p.read_text()
            if "—" in text or "–" in text:
                offenders.append(str(p))
    assert not offenders, f"em/en dashes remain in: {offenders}"


def test_numeric_ranges_survived_the_dash_sweep():
    """Ranges like 20-42 must not have collapsed into '20, 42'."""
    s = open("app/templates/settings.html").read()
    assert "20 to 42" in s and "0 to 21" in s
    assert "(°C, 20, 42)" not in s


def test_version_bump_carries_instead_of_producing_1_100():
    """1.99 must roll to 2.00, never 1.100.

    The scheme is strictly M.mm. '1.100' is rejected by parse_version, which
    makes is_newer() return False forever, which would silently freeze OTA
    for every deployed unit.
    """
    from app.ota.updater import is_newer
    from app.version import bump_bugfix, bump_feature, parse_version

    assert bump_bugfix("1.99") == "2.00"
    assert bump_feature("1.99") == "2.00"
    assert bump_feature("1.95") == "2.00"
    assert bump_bugfix("1.98") == "1.99"          # normal case untouched
    parse_version(bump_bugfix("1.99"))            # must stay parseable
    assert is_newer("2.00", "1.99") is True       # OTA still sees it as newer
