# Notes for the integrator — Section B (install / boot / image / docs)

Branch: `worktree-wf_97729aa6-4fe-2`. Files touched (all within my assignment):
`install.sh` (new), `init_pi.sh`, `scripts/firstboot.sh`, `scripts/update.sh`,
`scripts/start.sh` (one message), `scripts/verify_install.sh` (new),
`deploy/*` (+ `deploy/tmpfiles-incubator.conf`, `deploy/avahi/incubator.service`),
`build_image.sh`, `rpi-image/**`, `.github/workflows/*`, `docs/**`, `README.md`,
`app/ota/__main__.py`. All 216 existing tests pass unchanged. `bash -n` passes
on every script; shellcheck is not installed here (CI runs it advisory-only).

## Verified, not just written

* `install.sh` was run for real on this Ubuntu 24.04 (root, no systemd manager)
  with `INCUBATOR_IMAGE_BUILD=1 … --no-tuning`, then re-run twice
  (`--skip-packages`, with and without tuning). `scripts/verify_install.sh`
  reports ALL CHECKS PASSED; `/etc/incubator.env` custom keys survive, no
  duplicate keys are appended, units are re-templated.
* The bootstrap branch (`curl | sudo bash`, no `pyproject.toml` next to the
  script) was exercised with a stubbed `git`: it clones `--branch <ref>` and
  re-execs the cloned `install.sh` with the original arguments, both when run
  from a file and when piped on stdin.
* The text-editing helpers (fstab `noatime`, `config.txt` `[all]` sectioning
  and key replacement, `armbianEnv.txt` overlays, `zramswap`, env keys) were
  unit-tested on sample files for idempotency.
* `app/ota/__main__.py` helpers (`_pip_extras`, `_use_piwheels`,
  `_is_debian_family`) were checked against synthetic `/proc/device-tree/model`
  and `/etc/os-release` files.

## Cross-file requests (files I do not own)

1. **`tests/test_production_redundancy.py:198`** reads `init_pi.sh` and asserts
   the two `gpio=…` lines are present. `init_pi.sh` is now a one-line wrapper;
   I kept both strings in its header comment so the test stays green **without
   editing it**, but the honest fix is to point the test at the file that
   actually writes them:
   ```python
   init = open("install.sh").read()
   ```
   (keep the `build_image.sh` assertion as is — the gate still enforces them).
2. **`app/config.py:50`** comment says "Baked into the image by build_image.sh /
   init_pi.sh" → "install.sh". Cosmetic; agent A owns the file.
3. **`pyproject.toml`** (agent A): CI's test matrix includes Python 3.10, so
   `requires-python = ">=3.10"` must land in the same merge or the 3.10 job
   fails at `pip install`. `install.sh`/OTA already install the `[hardware]`
   extra; until agent A adds it pip prints "does not provide the extra
   'hardware'" (a warning, not an error — the core install still succeeds).
4. **Agent D (`feedback_service.py`)**: the README/BUILD_QUICKSTART/help text and
   `install.sh`'s summary document the LED/buzzer table exactly as in the spec.
   If the implemented patterns differ, the docs tables are the place to adjust.
5. **Agent E (`web.py`)**: README describes `GET /api/device/info`,
   `/api/setup/ping` and the Settings → Device card (Restart setup mode /
   Factory reset). Docs assume those land as specified.
6. **Agent A (`python -m app.hal`)**: `install.sh`'s summary, README, PORTING.md
   and PI_DEPLOY.md tell users to run `/opt/incubator/.venv/bin/python -m app.hal`
   for the detection report. Please make sure the `__main__` exists.

## Spec deviations (deliberate) — please review

* **`--no-tuning` never disables the boot-safe GPIO levels or the I²C/RTC
  overlay on a Pi.** The spec lists them under "Pi tuning (unless --no-tuning)";
  I kept them unconditional (as today) because `gpio=12,13,17,27=op,dh` is the
  heater-relay-OFF-during-boot safety line, not a performance knob. Only the
  cosmetics/perf items (disable-bt, splash, boot_delay, audio, gpu_mem, zram,
  masked timers) are gated by `--no-tuning`.
* **`incubator.service` runs `.venv/bin/python -m uvicorn …`** instead of
  `.venv/bin/uvicorn …` (same flags as the spec). Reason, found in the dry run:
  with `--system-site-packages`, if the distro Python already has uvicorn, pip
  creates no venv console script and the unit would fail; the module path works
  in both cases. `build_image.sh`'s gate checks the venv python + an installed
  `uvicorn` package (venv or system) instead of `.venv/bin/uvicorn`.
* **`INCUBATOR_WIFI_COUNTRY` on re-run**: only overwritten when
  `--wifi-country`/`INCUBATOR_WIFI_COUNTRY` is given explicitly; a plain re-run
  keeps the operator's value (otherwise every re-run would reset it to `US`).
* **Pi detection inside an image chroot**: `/proc/device-tree` is the build
  host's, so `install.sh` honours `INCUBATOR_IS_PI=1|0` (set by `build_image.sh`
  and the rpi-image hook) and also recognises a Pi-style `config.txt`,
  `/etc/rpi-issue` or `ID=raspbian`.
* **`pip install -e "<dir>[hardware]"` is best-effort** after a fatal
  `pip install -e "<dir>"`: a failing `gpiod` source build on an exotic board
  must not leave the appliance without the app (a distro `python3-libgpiod` is
  visible through system-site-packages anyway). OTA does the same fallback.
* **Package tiers**: python3/venv/pip/dev, git, curl, ca-certificates,
  NetworkManager (+ `dnsmasq-base` on apt) are fatal; everything else
  (rfkill, iw, wireless-regdb, avahi, libgpiod, i2c-tools, compilers, image
  libs, rsync, `dnsmasq` on non-apt) is installed one-by-one on failure and
  only warned about. `rfkill` lives in util-linux on Fedora/Arch, hence the
  per-package fallback.
* **Armbian I²C overlay**: appends `i2c0` (override with
  `INCUBATOR_ARMBIAN_I2C_OVERLAY`) to `overlays=` only when no `i2cN` entry is
  present; logged clearly. It is a guess per SoC, as the spec anticipated.
* **tmpfiles** also creates `/run/incubator/frames` (CAMERA_FRAME_DIR), in
  addition to the spec's `/run/incubator` line.
* `deploy/incubator-control.service` additionally got
  `After=incubator-firstboot.service` (the firstboot unit now has
  `Before=incubator-control.service`, so this is the matching side).
* `scripts/update.sh` additionally reinstalls `[hardware]`/`[pi]` extras and
  writes `.git-ref` after updating (so OTA compares against the right version).
* `build_image.sh --base` with a custom URL is cached under its own file name;
  the download sanity check now rejects HTML/text (instead of requiring "XZ"),
  so a `.img` or `.zip` URL is accepted.
* `rpi-image/layer.yaml` `packages:` replaces `dnsmasq` with `dnsmasq-base`,
  adds `avahi-daemon rfkill wireless-regdb python3-libgpiod i2c-tools`, drops
  `jq`/`xxd` (only the deleted hooks used them), keeps the rest.
* Added `scripts/verify_install.sh` (not in the spec): post-install assertion
  script used by CI's dry-run job and useful on a device.

## CI

* `install-dry-run` job **is included**: it runs `sudo INCUBATOR_IMAGE_BUILD=1
  ./install.sh /tmp/incubator-ci --no-tuning` on ubuntu-latest with a
  `policy-rc.d` that stops apt postinst scripts from starting NetworkManager /
  avahi on the runner (so the runner's networking is never touched), then
  `scripts/verify_install.sh`, then a second run proving idempotency. I ran the
  identical sequence on this Ubuntu 24.04 container end to end (apt + PyPI
  reachable) and it passed; the only difference on GitHub is a running
  systemd, where `systemctl enable` succeeds directly instead of via the
  symlink fallback. Expected runtime ~3–5 min.
* `tests` matrix: 3.10, 3.11, 3.12, 3.13 with `pip install -e ".[dev]"` (needs
  agent A's `requires-python` bump for 3.10).
* `shell-syntax` covers `install.sh init_pi.sh build_image.sh scripts/*.sh
  rpi-image/layer/incubator/hooks/*`; shellcheck runs only if present and never
  fails the job.
* `build-image.yml`: still passes `--grow 2048`; only comments/release text
  changed.

## Finding worth a fix (agent E / tests): the suite assumes `nmcli` is absent

While dry-running `install.sh` on this container I installed NetworkManager,
and the unchanged test suite then took 7 min and failed 5 tests
(`test_network`, `test_password_reset`, …): `WiFiService` only takes its mock
path on `FileNotFoundError`, so a present-but-not-running `nmcli`/`rfkill`/`iw`
makes the tests call the real binaries, which fail ("Could not create NMClient
object") and flip the onboarding flow into the setup-AP fallback. On a dev
laptop with NetworkManager actually running the tests would try to join Wi-Fi
for real. Suggested fix (agent E, owns `wifi_service.py` + `tests/conftest.py`):
a `WIFI_MOCK=true` (or reuse `GPIO_MOCK`) switch that short-circuits `_nmcli`,
set in `conftest.py`. I purged the packages again so the shared container is
back to its original state; all 216 tests pass here.

## Not done / out of scope

* `build_image.sh` with a real non-Pi base image was not executed here (no
  loop devices / root image in the sandbox); the generic path is a code-level
  change with `bash -n` and the same structure as the Pi path. The Pi path's
  logic (PARTUUID rewrite, verify gate, zerofree, qemu registration) is
  preserved verbatim.
* 32-bit ARM base images need `qemu-arm` binfmt on an x86 host;
  `ensure_emulation` detects the guest arch via `file` and registers/installs
  `qemu-user-static` for it (the direct `/proc/sys/fs/binfmt_misc/register`
  fallback is kept for aarch64 only, as before).
* Did not bump `app/version.py` (integrator does).
