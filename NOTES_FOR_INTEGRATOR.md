# Notes for the integrator — setup-flow back-end (section E + audit MUST-FIX 1–12)

Branch: `worktree-wf_802d80ea-310-2` (based on `b2b56ac`, wave 1 included).

## Signatures other agents rely on (all present)

* `OnboardingService(..., on_event: Callable[[str], None] | None = None)` — emits
  `setup_ready`, `connecting`, `connected`, `failed`. `_emit` never raises.
* `OnboardingService.start_manual_hotspot(device_id, source="button")` — the button
  handler can keep calling `start_manual_hotspot(device_id)`; the default `source`
  records setup-mode reason `button_hold`, which opens the onboarding routes to the
  LAN for 10 minutes (physical presence). The web route passes `source="web"`.
* `app.services.device_service.factory_reset(db, *, wifi_service, onboarding_service,
  setup_mode_service, device_id) -> dict` and `device_info(gpio_service=None,
  camera_service=None) -> dict`.
* `app.routes.web.set_runtime_services(..., gpio_service=None, camera_service=None)`
  — pass `gpio_service` / `camera_service` so `/api/device/info` and the Status chips
  read `backend_name` / `sensor_name` / `backend_resolved` (read with `getattr`
  defaults, so nothing breaks if they are missing).
* `SetupModeService()` with no argument now defaults to `settings.setup_state_path`.
* `WiFiService(country=None, *, iface=None, ap_ip=None)` — new: `forget_all_networks()`,
  `has_ap_support()`, `has_saved_profile(ssid)`, `.iface`, `.ap_ip`; module-level
  `nm_available()`, `reset_nm_probe_cache()`, `wifi_mock_allowed()`, `valid_ssid()`.

## Cross-file requests (files I do not own)

1. **app/main.py (agent D)**
   * `OnboardingService(..., on_event=feedback.event)`.
   * `set_runtime_services(..., gpio_service=gpio_service, camera_service=camera_service)`.
   * `/health`: optionally add `"nm_available": wifi_service.network_debug().get("nm_available")`
     (cheap; the probe is cached 60 s) so a box without NetworkManager is visible.
   * `app/control/bridge.py daemon_reading()` could pass through `heater_on` / `fan_on`
     from the state file — `main.py:407` reads `reading.get("heater_on")` for the INA219
     check and today always gets `None`. (Audit side-note; not required for E.)
2. **app/static/js/onboarding.js (agent C)** — mirror the new device-side SSID rule
   (any printable non-control characters, 1–32 **bytes** of UTF-8). Replace
   `const SSID_OK_RE = /^[\w\s\-\.@#!]{1,32}$/;` with:

   ```js
   // Mirrors WiFiService.valid_ssid: printable, no control chars, 1–32 UTF-8 bytes.
   const SSID_OK_RE = /^[^\u0000-\u001F\u007F-\u009F]{1,32}$/u;
   function ssidOk(s) {
     if (!SSID_OK_RE.test(s) || !s.trim()) return false;
     try { return new TextEncoder().encode(s).length <= 32; } catch { return s.length <= 32; }
   }
   ```
   and use `ssidOk(selectedSsid)` in `wifiStepValid()`. (`tests/test_farmer_flow.py`
   only asserts the `SSID_OK_RE` identifier exists, so the name should stay.)
3. **deploy/incubator.env.example + docs (agent B)** — document two new knobs:
   `INCUBATOR_WIFI_MOCK=true` (simulate Wi-Fi when NetworkManager is absent on a dev
   box; `GPIO_MOCK=true` implies it) and `INCUBATOR_NMCLI_BIN=/path/to/nmcli`
   (override the nmcli binary; the test suite sets it to a non-existent path).
   Also mention: without NetworkManager a *real* device now logs
   `ERROR … NetworkManager is not available` and reports no hotspot/connection instead
   of pretending.

## Deliberate behaviour changes (spec deviations / judgement calls)

* Setup mode is entered **only once the AP is really up** (boot, button, web, fallback).
  A failed AP start sets a retry flag and the watchdog retries it on every poll
  (MUST-FIX 5); it never leaves a stale `setup_mode: true` that would unlock
  `/api/reset-password`.
* `/onboarding` page context `setup_mode` is `setup_mode_service.is_setup_mode() OR the
  device has never been set up (no Wi-Fi saved and no account)`. The template treats
  `setup_mode` as "show the wizard"; a never-configured device reached over Ethernet or
  on a dev laptop (auto-hotspot off) must still get the wizard, and
  `tests/test_frontend.py::test_onboarding_defaults_to_setup_mode_without_new_context`
  asserts exactly that. `hotspot_active` is always accurate.
* The watchdog's periodic fallback rejoin does **not** emit `failed` when the saved
  network is still unreachable (it would buzz every 10 minutes at night); the AP coming
  back emits `setup_ready` as before. Operator-initiated joins (`complete()`,
  `switch_network()`) do emit `failed`.
* `/api/settings` never writes heater/fan pins from the web process. Without the control
  daemon the toggle is applied through `main.execute_hardware_action` (same pin owner as
  before). With the daemon, only an **OFF** is forwarded immediately (fail-safe
  direction); an ON is left to the daemon, which re-reads `heater_enabled` /
  `fan_enabled` every iteration — forcing ON from the web would override its thermostat.
* `/onboarding/complete` with `create_account=true` returns **409** for an anonymous
  caller once any account exists — even on the AP. A signed-in owner may still add one.
* `/api/network/connect` while the AP is up answers immediately (`{"ok": true,
  "switching": true}`) and runs `OnboardingService.switch_network` as a BackgroundTask;
  on failure the DB keeps the previous SSID if the device fell back onto it, else clears
  it (recovery wizard). On the LAN the call stays synchronous (502 on failure).
  `switch_network(ssid, password, *, previous_ssid=None)` tries the previous network
  before raising the AP, so a typo never takes a working device offline.
* `ensure_reachable` starts joins/rejoins on a daemon thread and waits at most 1 s for it
  (`_transition_join_timeout`) so mocked transitions settle synchronously for the existing
  tests while a real nmcli join keeps running in the background. One `RLock` serialises
  `complete()`, `switch_network()`, the worker and `start_manual_hotspot()` (which waits
  up to 5 s and then returns `{"ok": false, "error": "busy"}`).
* `get_connected_ssid()` excludes our own AP (the SSID we broadcast, the
  `<prefix>-XXXX` shape, or an active `incubator-hotspot` connection).
* `_purge_ap_connections` only deletes profiles that are `mode=ap`, named exactly
  `incubator-hotspot` / `Hotspot`, or match `<prefix>-XXXX`. "Johns Hotspot" survives.
* `forget_all_networks()` keeps the `incubator-hotspot` profile (the AP is managed by
  start/stop_hotspot; a reset performed over the AP must not drop the operator).
* `/api/vision/analyze` additionally restricts `image_path` to the capture roots used by
  the Testing tab (`captures_dir`, `camera_image_dir`, testing uploads) — 400 otherwise.

## Tests

* 485 pass (385 at the base + 100 new). New files: `tests/test_wifi_service_hardening.py`,
  `tests/test_setup_mode_service.py`, `tests/test_onboarding_watchdog.py`,
  `tests/test_device_service.py`, `tests/test_setup_routes.py`.
* **One existing assertion adapted** (spec-driven):
  `tests/test_frontend.py::test_onboarding_defaults_to_setup_mode_without_new_context`
  asserted the old back-end's empty `wizardCandidates` list (`[]`). Section E adds
  `candidate_urls` to the `/onboarding` context, so the test now checks the tag exists
  and carries a `.local:8000` URL. Everything else is untouched.
* `tests/conftest.py` now sets `INCUBATOR_NMCLI_BIN=/nonexistent/nmcli` and a
  per-process `INCUBATOR_SETUP_STATE_PATH`, resets the NetworkManager probe cache per
  test and clears AP/setup-mode state after each test. The suite no longer depends on
  nmcli being absent from PATH and can never touch a host's real Wi-Fi.

## Commit trailers

The harness running this session requires `Co-Authored-By` / `Claude-Session` trailers
on every commit. The project's own rule is "no AI attribution in commits"; strip the
trailers when squashing/merging if that rule stands (`git commit --amend` or
`git filter-branch --msg-filter`).

## Audit findings rejected / not applicable (one line each)

* **web.py L271** (dashboard CTAs lead to a wizard that raises the AP) — front-end
  feature outside E; the wizard now needs an explicit "Start setup mode" tap (and a login
  once an account exists), and the "Start Setup" tile is hidden once setup is complete.
* **web.py L514** (make `/api/reset-password` require the AP subnet + auto-expire) — not
  changed: `tests/test_password_reset.py` unlocks reset with an arbitrary setup-mode
  reason; the exploit chain is closed by gating `/onboarding/start` and by never entering
  setup mode unless the AP is actually up.
* **wifi_service.py L30** ("no leading/trailing whitespace" in SSIDs) — such SSIDs are
  legal and reach nmcli as argv; only control characters, blank names and >32 bytes are
  rejected.
* **wifi_service.py L244** (write the dnsmasq hijack config from the app) — installer
  territory (agent B); the app now pins `ipv4.addresses` to `settings.ap_ip` so every
  printed URL and the captive responder agree with NetworkManager.
* **onboarding_service.py L112** (bounded wait for the Wi-Fi device in `boot()` +
  `nm-online` ExecStartPre) — a startup wait would delay READY and the UI; the watchdog
  retries the AP every poll instead; the unit file belongs to agent B.
* **web.py L356** (relabel dashboard chips "Heater enabled / Heating now") — templates
  are agent C's; `live.heater`/`live.fan` now carry the daemon's real output state in
  daemon mode and `live.heater_enabled`/`fan_enabled` are provided for a future relabel.
* **conftest.py L9** (monkeypatch `WiFiService._nmcli` in conftest) — kept the real
  `WiFiService` exercised over HTTP with nmcli pointed at a non-existent binary and the
  explicit mock path: same isolation, more coverage.
* **wifi_service.py L295** ("filter only the exact SSID we broadcast" in scans) —
  partially: the `<prefix>-XXXX` shape and the broadcast SSID are hidden
  (`tests/test_wifi_scan.py` requires "Incubator-A3F2" hidden with no AP running);
  "IncubatorHome" / "Johns Hotspot" are now offered.
