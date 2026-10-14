# Notes for the integrator — Section C (web front-end)

Worktree branch: `worktree-wf_97729aa6-4fe-3`. All 216 pre-existing tests pass plus 23 new ones in
`tests/test_frontend.py` (239 total). JS files all pass `node --check`.

## What was delivered (my files only)

- `app/static_files.py` (new): `CachedStaticFiles` (Cache-Control immutable/1 y when `?v=` is present,
  1 h otherwise, header kept on 304) and `add_web_middleware(app)` (GZip, min 1 KiB, level 6).
- PWA: `app/static/manifest.webmanifest`, `app/static/icons/{icon.svg, icon-192.png, icon-512.png,
  apple-touch-icon.png}` (PNGs rendered with Pillow, 6–30 KB each; maskable-safe). `base.html` carries
  manifest / theme-color / apple-touch-icon / app-capable metas and `?v={{ version }}` on every asset.
- Nav: top links Home / Controls / Status / Help; profile menu Settings, Network settings, Vision lab,
  Setup / change Wi-Fi, Logout; fixed bottom tab bar ≤620 px (44 px targets, safe-area padding, body
  bottom padding). `base.js` marks the active link in both bars; every element lookup is guarded.
- Onboarding wizard rewritten (`templates/onboarding.html`, `static/js/onboarding.js`): pane 0 "Change
  Wi-Fi / run setup again" (not in setup mode), Step 1 Wi-Fi (list with lock + signal bars, Refresh,
  manual SSID, inline password only when needed, fail-fast checks), Step 2 Finish up (name + collapsed
  "Protect with a login" toggle, big Connect), Connecting screen (staged checklist; probes every 3 s for
  180 s with `fetch(..., {mode:'cors', cache:'no-store'})` + 2.5 s AbortController; candidate set =
  template `candidate_urls` ∪ response `candidate_urls` ∪ `app_url` ∪ `http://<hostname>.local:8000`;
  AP address probed too and treated as "hotspot came back" only after 25 s; candidate answers are
  trusted only when the ping payload says `hotspot_active: false`, or — for a back-end without that
  field — after the 25 s window, because captive DNS resolves every name to the AP while it is up).
  Found → "Open my incubator" + 5 s cancellable auto-open + LAN-IP tip from `ips[]`; 180 s → manual
  instructions with http-safe copy fallback. XSS fixed: SSIDs rendered with createElement/textContent
  in both `onboarding.js` and `network.js` (regression test asserts no `innerHTML` in either file).
  `identity.js` linked-account email is now text as well. Retry mode still skips Step 2.
- Dashboard: dismissible first-run welcome card (only when `home_summary.setup_complete`), template-side
  fallbacks "Incubator" / "unclaimed" for the old "Pi Zero Incubator" / "PI-UNCLAIMED" defaults, metrics
  card first with bigger numbers on phones, polling pauses while the tab is hidden.
- Settings: new "Device" card (`#device`) — info chips from `GET /api/device/info`, address list with copy
  buttons, "Restart setup mode" (confirm → `POST /onboarding/start`), "Factory reset…" (type RESET →
  `POST /api/device/factory-reset` `{confirm:'RESET'}`). Both endpoints 404 today → graceful messages.
- Help page rewritten (tabs, LED/buzzer table from spec B, setup button 4 s / 15 s, finding the address,
  automatic updates, troubleshooting, candling). Status page shows Board / GPIO driver / Sensor chips when
  `health.board` / `health.gpio_backend` / `health.sensor_backend` are present (defaults otherwise).
- Update popup text replaced per spec. "Testing"/"Hardware" wording → "Vision lab"/"Controls" in templates.

## Jinja defaults for variables the back-end does not send yet

`onboarding.html`: `hotspot_active` (undefined ⇒ setup mode), `setup_mode`, `has_account`, `hostname`,
`candidate_urls` (`[]`), `ping_path` (`/api/setup/ping`). `help.html`: `factory_reset_hold_seconds`
(15), plus defaults for `hold_seconds`, `ap_ssid_prefix`, `ap_ip`. `status.html`: `health.board`,
`health.gpio_backend`, `health.sensor_backend`. `base.html` and all script tags: `version` (`''`).

## Existing test adapted (spec changes behaviour) — tests/test_farmer_flow.py

`test_wizard_is_six_steps_with_inline_password` → renamed `test_wizard_is_two_steps_with_inline_password`.
The wizard is now 2 numbered steps + a connecting pane (panes `data-step` 0–3), so the assertions became
`'data-step="3"' in html and 'data-step="4"' not in html` and `"Step 1 of 2" in html`; the
`wifiPasswordWrap` assertion is unchanged. This is the only edit outside my assigned files. Everything
else in `test_farmer_flow.py` and `test_wifi_join.py` (incl. `test_finish_screen_never_auto_navigates_away`)
passes unchanged: the JS still contains "Keep this page open" and `window.open(appUrl`, and never
navigates to the hotspot IP — the only auto-navigation happens after the device has answered from the
home network.

## Cross-file requests (files I must not edit)

1. **app/main.py (agent D)** — replace the static mount and add the middleware:
   ```python
   from .static_files import CachedStaticFiles, add_web_middleware
   app.mount("/static", CachedStaticFiles(directory=str(BASE_DIR / "static")), name="static")
   add_web_middleware(app)   # before the app starts serving (middleware stack is built lazily)
   ```
2. **app/routes/web.py (agent E)** — as per spec: onboarding context (`hotspot_active`, `setup_mode`,
   `has_account`, `hostname`, `candidate_urls`, `ping_path`), `/onboarding/complete` response
   (`candidate_urls`, `ping_path`, keep `app_url`/`hostname`/`wifi_ssid`), `GET /api/setup/ping` with
   `Access-Control-Allow-Origin: *` **and** a `hotspot_active` boolean in the payload (the wizard uses it
   to reject answers served through the captive DNS hijack while the AP is still up), `ips[]` for the
   Android tip. Dashboard defaults: `device_name = "Incubator"`, `device_id = "unclaimed"` (the template
   already maps the old strings, so this is cosmetic). Please also pass `factory_reset_hold_seconds`
   (`int(settings.factory_reset_hold_seconds)`) in the `/help` context — it defaults to 15 otherwise.
   Quick-action labels on the dashboard come from `web.py` (`"Hardware"` → suggest `"Controls"`,
   `"Manual GPIO test panel"` → `"Lock, door, turner, candling"`).
3. **app/services/captive_portal.py (agent E)** — no change required; the landing page still links to
   `/onboarding`, which now lands directly on Step 1 in setup mode.

## Behavioural notes / small deviations

- Pane 0 includes an extra "Continue the setup on this page" link (shown after "Start setup mode") for
  boards reached over Ethernet or on a dev laptop where no AP exists; the spec's primary flow is unchanged.
- `apiRequest` in `base.js` now maps a bare 404 (route missing) to "This feature is not available on this
  software version." so the Device card degrades cleanly on today's back-end.
- `settings.html`: the old `#device` anchor (which pointed at the targets form) now points at the new Device
  card; the targets form heading is `#controls`. The profile menu no longer links to `#device`.
- No service worker on purpose (local-only device; a cached shell would hide real state).
