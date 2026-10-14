# Notes for the integrator — agent D (services onto the HAL + audit MUST-FIX)

Branch `worktree-wf_802d80ea-310-1`, based on `b2b56ac` (wave 1). 533 tests pass
(385 baseline + 148 new) with `PYTHONPATH=. .venv/bin/python -m pytest -q`.

## Base-commit note
The worktree was created at `fa42c4f` (main) without wave 1; it was reset to
`b2b56ac` (`claude/portable-appliance-setup`) before any work, as the task
required building on wave 1. No wave-1 work was redone.

## Files touched (all owned by D)
`app/main.py`, `app/control/{__main__.py (new), loop.py, bridge.py}`,
`app/services/{gpio,hardware,button,camera,alert,storage,cloud,feedback (new)}_service.py`,
`app/database.py`, `app/models.py`, `app/ota/{__main__.py, updater.py}`, `app/version.py`,
tests: `test_version.py`, `test_control_entrypoint.py`, `test_database_tuning.py`,
`test_gpio_service.py`, `test_control_safety.py`, `test_button_service.py`,
`test_feedback_service.py`, `test_hardware_service_daemon_mode.py`, `test_alert_silence_scope.py`,
`test_camera_backends.py`, `test_ota_apply.py`, `test_storage_fast_path.py`,
`test_cloud_lazy_import.py`, `test_main_wiring.py` (new); `test_control_loop.py` (one assertion adapted).

## Existing test adapted (deliberate behaviour change)
* `tests/test_control_loop.py::test_command_file_is_drained_and_applied` asserted the
  command file is *truncated* after a drain. MUST-FIX 6 replaces truncate with claim-by-rename
  (`os.replace(path, path + ".processing")`, read, unlink), so the test now asserts the file (and
  the `.processing` file) no longer exist.

## Interfaces other agents rely on (all present)
* `GPIOService.backend_name`, `.sensor_name`, `.is_mock`, `.mock_fallback`, `.describe()`;
  `CameraService.backend_resolved`, `.describe()` — for `device_service.device_info`.
* `main.py` passes `on_event=feedback_service.event` to `OnboardingService` and
  `gpio_service=/camera_service=` to `set_runtime_services` **only if the signature accepts them**
  (`_accepted_kwargs` inspects the signature; no try/except). Once agent E's kwargs land they are
  picked up automatically; nothing to change in main.py.
* `_on_button_held("factory_reset")` imports `app.services.device_service` lazily and calls
  `factory_reset(db, wifi_service=, onboarding_service=, setup_mode_service=, device_id=)`; a
  missing module logs an error and plays the `failed` chirp.
* `app.control.bridge.daemon_reading()` now also returns `heater_on`, `fan_on`, `sensor_fault`,
  `mock`, `last_turn_ts`, `fresh` (`heater_on`/`fan_on` are `None` when the state file is stale).
* `AlertService.alert_state()` adds `silenced_types: [...]` and a per-alert `silenced` flag;
  `silenced` (bool) now means "everything active is muted".
* `HardwareService(gpio, camera, daemon_state_path=...)`: in daemon mode `read_*` return the
  daemon reading and `get_state()` reports the daemon's heater/fan (`None` when stale) — the
  Status page's "Output state" chips therefore become truthful in daemon mode with no route change.

## Cross-file requests (files I do not own)
1. **`app/config.py`** (agent A): `camera_backend` default is still `"picamera2"`; the env example
   already says `CAMERA_BACKEND=auto` and `CameraService` now supports `auto`. Suggest
   `os.getenv("CAMERA_BACKEND", "auto")` with the comment `# auto | picamera2 | opencv | mock`.
2. **`deploy/incubator-ota.service`** (agent B): add `EnvironmentFile=/etc/incubator.env` and
   `WorkingDirectory=__INSTALL_DIR__`. Without them `python -m app.ota` never sees
   `CONTROL_DAEMON_ENABLED`/`CONTROL_STATE_PATH`, so `_verify()`'s daemon-freshness gate is dead
   code (audit, scripts/ota-agent.sh L27). Alternatively `set -a; source …; set +a` in
   `scripts/ota-agent.sh`.
3. **`build_image.sh` verify gate** (agent B): run
   `chroot "$r" /opt/incubator/.venv/bin/python -m app.control --check` (new flag, exit 0, no
   GPIO/DB access) so a broken daemon entry point can never ship again.
4. **`app/routes/web.py`** (agent E): the status page passes `"gpio_mock": settings.gpio_mock`;
   prefer the effective value, e.g. `_hardware_service.gpio.is_mock if _hardware_service else
   settings.gpio_mock`, so a silent mock fallback shows "GPIO: simulated". `/health` already does.
   Also: `api_settings_update` still calls `_hardware_service.set_heater/set_fan` directly — in daemon
   mode these now return `{"ok": False, "error": "not_owned"}` (harmless, nothing is driven), but the
   call should go through `execute_hardware_action` or be dropped (audit web.py L696).
5. **`pyproject.toml`** (agent A): `email-validator` is listed but `EmailStr` is unused (audit
   cloud_service L9); optional cleanup.
6. **Docs** (agent B): heat-runaway behaviour changed — document "heater forced off for 15 min; if
   the chamber cooled ≥0.3 °C it resumes, otherwise it stays off and re-checks every 15 min"; and the
   setup button: 4 s = setup mode, 15 s = 3 rapid beeps (armed), **release** = factory reset.
7. **Commit attribution**: the harness instructed me to end each commit with `Co-Authored-By` /
   `Claude-Session` trailers, while the task text asked for no AI attribution. The trailers are in
   the commit messages only (no code/docs). Strip them on squash/rebase if the repo policy is "none".

## Spec deviations / judgement calls
* **Runaway latch clear** follows the spec (cool ≥0.3 °C in 15 min while OFF → clear), not the
  audit's "rise ≥0.5 above / fall below target−2" variant. While latched the state still reports
  `online: False` + `sensor_fault: "heat_runaway"` (as before) so the alert engine raises
  `sensor_offline` and the farmer hears it. No `heater_reset` bridge command was added (not in spec).
* **Settings outage**: the loop keeps regulating on the last good snapshot (or defaults) with the LIVE
  sensor; I did not add the audit's "fail-safe after N failures" because a locked DB says nothing about
  the chamber and chilling eggs for a DB hiccup is the worse outcome. Logged once per outage/recovery;
  state file carries `settings_ok`.
* **Turn commands**: only a full turn (≥200 steps) resets `last_turn_ts`; a 1-step nudge from the
  Controls page no longer postpones the next scheduled turn (audit loop.py L225).
* **Mock fallback**: a GPIO backend that resolves to mock without `GPIO_MOCK` logs a WARNING, is
  exposed (`backend_name == "mock"`, `mock_fallback`, `/health gpio_mock: true`) and the sensor is NOT
  mocked (readings are `no_sensor`/`ok=False`). I did not make it refuse to start (spec: never crash).
* **`_default_settings_provider`** uses stdlib `sqlite3` (read-only URI, busy_timeout 5 s) for sqlite
  URLs; a non-sqlite URL (none shipped) falls back to the ORM lazily. A subprocess test proves
  `sqlalchemy`, `app.database` and `app.models` never load in the daemon.
* **Startup ordering** (audit main.py L612) was left as today except the retention sweep (first run
  ≥1 h after boot) — `sd_ready()` stays first and the synchronous poller prime stays (spec: keep the
  poller as today; several tests rely on `_poll()` semantics). The watchdog is now pinged before AND
  after each poll.
* **Camera**: the single-producer-thread / idle-stop refactor from the audit was not done (not in
  spec); FrameDurationLimits + buffer_count=2, the always-yielding stream and the device scan are.
  No `CAMERA_DEVICE` env knob (config.py not mine) — `CameraService(video_devices=[...])` exists.
* **`version.py`**: kept the `M.mm` scheme (OTA tag regex, bump script, image names all depend on
  it) and made bumps roll into the next major; did not move to 3-part semver. `VERSION` unchanged.
* **`GPIOService.get_state()`** dropped the never-used `temp_c`/`humidity_pct` keys and added
  `backend`, `mock`, `owned`.

## Rejected / not-applicable audit findings (my files)
* gpio_service L83 "refuse to start on mock fallback" — rejected: spec requires graceful degradation;
  implemented warn + expose + no fabricated readings instead.
* loop.py L253 sub-suggestion "apply_failsafe after N settings failures" — rejected (see above).
* loop.py L342 "heater_reset command" and the alternative clear rule — superseded by the spec rule.
* main.py L612 "move sd_ready to the end / drop the synchronous prime" — deferred: spec says keep the
  poller as today; READY-first protects slow first boots from a start-timeout kill.
* camera_service L136 "single producer thread + stop camera after 60 s idle" — deferred (not in spec).
* version.py "switch to M.m.p semver / widen tag regex" — rejected; rollover fixes the boundary.
* scripts/ota-agent.sh, deploy/incubator-ota.service, build_image.sh, init_pi.sh findings — not my
  files; forwarded above where they matter to code I changed.
