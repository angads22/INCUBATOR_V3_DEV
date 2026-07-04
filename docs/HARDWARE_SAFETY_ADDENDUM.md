# Hardware Safety Addendum — Independent Thermal Cutoff & RTC

Two redundancy items surfaced by the reliability audit that live **below the
software layer**. The firmware already fails safe (heater OFF on sensor loss,
hardware watchdog reboot, DB self-heal), but two failure modes cannot be closed
in software alone:

1. **A welded heater relay / shorted SSR** — the MCU commands OFF but current
   keeps flowing. No amount of software can open a fused contact.
2. **No wall clock across a power cut** — the Pi Zero has no battery-backed
   RTC, so incubation day-count, turn timestamps, and log ordering drift until
   Wi-Fi/NTP returns. A farmer on intermittent internet can hatch on the wrong
   day.

This document specs the parts, wiring, trip points, and the (small) firmware
touchpoints for each. Part 1 is pure BOM + optional diagnostics code. Part 2 is
BOM + OS-config changes I can ship in the image.

Current wiring for reference (`app/services/gpio_service.py`, `app/config.py`):

| Function        | BCM pin | Header pin | Notes                          |
|-----------------|---------|------------|--------------------------------|
| Heater relay    | GPIO17  | 11         | active-LOW, mechanical relay   |
| DHT22 data      | GPIO4   | 7          | **only** temp/humidity sensor  |
| Fan relay       | GPIO27  | 13         | active-LOW                     |
| I²C SDA / SCL   | GPIO2/3 | 3 / 5      | **free — nothing on the bus**  |

Setpoint 37.5 °C, hysteresis ±0.4 °C, high-temp alarm ≈ 39 °C, control loop
10 s. These numbers set the trip windows below.

---

## Part 1 — Independent Over-Temperature Cutoff

### Failure mode being closed

The heater is switched by a mechanical relay on GPIO17. Relay contacts can
**weld closed** under inrush/arcing; solid-state relays (if substituted)
overwhelmingly fail **shorted**. In either case the control daemon's
`set_heater(False)` and `failsafe_outputs()` (`app/control/loop.py`) are
electrically meaningless — the element runs at 100 % duty until something melts.
A DHT22 read failure makes it worse: the loop can no longer even *see* the
runaway.

The fix is a thermal interrupter **in series with the heater's switched leg,
physically downstream of the relay**, that opens on temperature with zero
dependence on the Pi, the relay, or firmware.

### Two-layer cutoff (defense in depth)

Put **both** devices in series in the heater leg. Either one opening kills the
heater.

```
              relay (GPIO17)      L1: bimetal limit      L2: one-shot TCO
   heater  o──/ ──────────────────/‾ ─────────────────────/  ─────────o  heater
   supply +   contacts             (manual-reset,          (thermal fuse,        element
                                    opens ~57 °C,           opens ~77 °C,
                                    on chamber air)         bonded to element)
```

**Layer 1 — regulating limit (resettable):** a manual-reset bimetal thermal
switch (KSD301-style), **normally-closed, opens at ~55–60 °C**, mounted in the
incubation-chamber airstream near the element. Chosen ~18–20 °C above the 39 °C
high-temp alarm so it never nuisance-trips in normal operation, but well below
any temperature that cooks a full clutch or the enclosure. Manual-reset (not
self-resetting) is deliberate: a trip should force a human to inspect the unit,
not silently cycle. When it trips, the appliance can still report the fault (see
diagnostics) and the farmer presses the reset button after fixing the cause.

**Layer 2 — ultimate backstop (one-shot):** a UL/VDE-recognized thermal cutoff
(TCO / thermal fuse), **Tf ≈ 72–77 °C**, bonded directly to the heater element
body (not the air) with high-temp epoxy or a mechanical clamp. It is
non-resettable: if it ever blows, the element is replaced along with the fuse.
This is the "the bimetal also failed" layer — cheap insurance that the element
can never sustain a fire-risk temperature.

Trip-point ladder, coldest to hottest:

| Level                         | Temp     | Resets?        |
|-------------------------------|----------|----------------|
| Setpoint                      | 37.5 °C  | —              |
| High-temp software alarm      | ~39 °C   | auto           |
| **L1 bimetal limit**          | ~57 °C   | manual button  |
| **L2 thermal fuse (backstop)**| ~77 °C   | replace part   |

### Electrical sizing

Size both devices for the heater current with ≥2× margin and for the supply
voltage. Two supported variants:

**Recommended — low-voltage DC heater (farmer-safe).** A 12 V or 24 V PTC /
ceramic / film heater. Both cutoffs go in the **positive DC leg** between the
relay and the element. For a 40 W @ 12 V element (~3.4 A), use devices rated
≥6 A. No mains inside the reachable enclosure.

**Mains heater (only if the design already uses one).** Cutoffs go in the
**switched line (live) conductor**, downstream of the relay. Use 250 VAC-rated
parts sized for element wattage / mains voltage × 2. This path must be built and
tested to the relevant appliance safety standard; keep all mains wiring in a
sealed compartment behind a tamper-resistant panel.

> If the current design switches a mains heater directly off the relay, moving
> to a low-voltage element is the single biggest safety upgrade available and is
> strongly recommended before shipping to farmers.

### Optional diagnostics (firmware touchpoint)

Purely hardware, the cutoff is silent — the unit just stops heating and the
software eventually raises the existing **low-temperature** alarm. That's safe
but opaque. To turn it into an actionable "cutoff tripped — inspect unit" fault,
add current sensing on the heater leg:

- **Part:** ACS712 (5 A/20 A hall-effect, analog) via an MCP3008 ADC, **or** an
  INA219 (I²C, address 0x40) shunt monitor on the heater leg. INA219 is tidier —
  it shares the same free I²C bus as the RTC.
- **Logic:** in `ControlDaemon.loop_once()`, when `self._heater_on is True` but
  measured heater current ≈ 0 A for >1 iteration, emit a fault:
  `ActionLog(action="hardware.thermal_cutoff_tripped")`, latch `set_alarm(True)`,
  and surface a persistent banner via the existing alert engine
  (`app/services/alert_service.py`). Symmetrically, current flowing while
  `self._heater_on is False` means a **welded relay** — same alarm, different
  message. This is the software-visible confirmation that Layer 1/Layer 2 or the
  relay has faulted.
- No UI redesign: reuse the existing alert banner + buzzer paths.

**Implemented.** `GPIOService.read_heater_current()` reads the INA219 shunt
register directly (no extra pip deps — `smbus2`/`smbus`, lazy-opened, fully
optional), and `AlertService.evaluate_heater()` raises a debounced, latched
`hardware.heater_cutoff` / `hardware.heater_stuck` alarm through the existing
buzzer path. Gated by `HEATER_CURRENT_SENSE_ENABLED` (default off), so a unit
without the sensor never touches the I²C bus. Add the INA219 and flip the flag.

### BOM — Part 1

| Item | Example part | Spec | ~Qty | ~Cost |
|------|--------------|------|------|-------|
| Bimetal limit switch (L1) | KSD301, manual-reset, N.C. | Open ~57 °C, ≥6 A / rated V | 1 | $1–2 |
| Thermal fuse / TCO (L2) | Cantherm/NEC SEFUSE, UL-recognized | Tf ~77 °C, ≥6 A, rated V | 1 | $0.50–1 |
| High-temp bonding | Thermal epoxy / clamp | to element body | 1 | $1 |
| *(optional)* current sensor | INA219 I²C module | shares RTC I²C bus | 1 | $2–4 |

Total added cost: **~$3** for the cutoff, ~$6 with diagnostics.

### Validation procedure

1. **L1 trip test:** with eggs removed, force the heater ON and block airflow /
   apply heat until the chamber sensor reads the L1 rating; confirm the heater
   circuit opens and stays open until manual reset.
2. **L2 trip test (destructive, one sample):** on a sacrificial build, drive the
   element with L1 bypassed and confirm the TCO opens before the element passes
   its rated surface temperature.
3. **Diagnostics:** if fitted, trip L1 during normal operation and confirm the
   UI raises `hardware.thermal_cutoff_tripped` and the buzzer latches.

---

## Part 2 — Battery-Backed Real-Time Clock (RTC)

### Failure mode being closed

The Pi Zero 2W has no onboard RTC. After a power cut it boots with the clock
frozen at the last `fake-hwclock` save (or the image build date) until
`systemd-timesyncd` reaches an NTP server over Wi-Fi. Everything time-derived
drifts during that window:

- **Incubation day-count** (chick vs. duck vs. quail schedules) — wrong day =
  wrong lockdown/hatch timing.
- **Egg-turn scheduler** (`turn_due()` compares `now_ts` deltas) — a backward
  clock jump can delay or skip a turn.
- **Event log / candling timestamps** — out of order, hard to trust.

A farmer with no or intermittent internet may *never* get an NTP fix, so the
clock stays wrong for the whole hatch.

### Part & bus

**DS3231 I²C RTC module** (ZS-042 "AT24C32" board or Adafruit PiRTC).

- TCXO, ±2 ppm → ~1 minute/year, temperature-compensated across the incubator's
  range (the cheaper DS1307/PCF8523 drift far more — DS3231 is the right pick).
- I²C address **0x68**; the onboard EEPROM (if present) is 0x57 — neither
  collides with the INA219 (0x40) from Part 1.
- **CR2032 coin-cell** backup holds time with the Pi fully unpowered.

> **Module gotcha:** many ZS-042 boards include a diode+resistor charging
> circuit intended for a rechargeable LIR2032. With a non-rechargeable CR2032
> that circuit slowly ruins the cell. Either fit a LIR2032, or remove the charge
> resistor (typically marked `201`) — a 30-second desolder. Adafruit's board
> ships without the charge circuit and needs no rework.

### Wiring

| RTC pin | Pi header pin | BCM  |
|---------|---------------|------|
| VCC (3V3) | pin 1       | 3V3  |
| GND       | pin 9       | GND  |
| SDA       | pin 3       | GPIO2 |
| SCL       | pin 5       | GPIO3 |

The I²C bus is currently unused, so no contention with the DHT22 (GPIO4,
1-wire), the relays, or the camera (CSI). If Part 1's INA219 is fitted it shares
this bus.

### OS / image integration (I can ship this)

Changes to `init_pi.sh` / boot config — no app redesign:

1. **Enable I²C + load the RTC overlay** in `/boot/firmware/config.txt`:
   ```
   dtparam=i2c_arm=on
   dtoverlay=i2c-rtc,ds3231
   ```
   The kernel then binds the DS3231 as `/dev/rtc0` at boot and `hwclock` reads it
   before the app starts.

2. **Keep `fake-hwclock` ENABLED (deliberate).** The standard single-board
   guidance disables it, but this is a mixed fleet: a farmer may or may not fit
   the DS3231. `fake-hwclock` is the clock fallback for units **without** an RTC,
   so disabling it would regress them (worse clock, not better). A fitted DS3231
   takes precedence at boot via the kernel `rtc` class regardless, so the two
   coexist safely. **Implemented this way** in `init_pi.sh`.

3. **Write-back on NTP sync:** with `systemd-timesyncd` active and the overlay
   loaded, the kernel syncs system→RTC automatically once NTP lands. No cron
   needed on modern Bookworm; a `hwclock -w` in the timesync `ExecStartPost` is
   belt-and-suspenders if desired.

4. **Image gate:** `verify_artifact()` in `build_image.sh` asserts the two
   config.txt lines (`dtparam=i2c_arm=on`, `dtoverlay=i2c-rtc,ds3231`) — same
   zero-tolerance pattern as the existing checks. **Implemented.**

### Software robustness (defense in depth — worth doing regardless)

Even with the RTC fitted, make the incubation math clock-jump-tolerant so a dead
coin-cell or a bad NTP step can't corrupt a hatch (**all three implemented**):

- **Incubation day comes from the vision model, not the wall clock.** The
  development day-estimate is produced per-candling by `predict_stage()` and the
  growth timeline uses observation *deltas*, so day-of-incubation is already
  robust to an absolute-clock jump. Audited — no schedule logic depends on
  uptime/monotonic-only, so no artificial `started_at` anchor was needed.
- **Boot-time clock sanity:** `_detect_clock_rollback()` in `app/main.py`
  compares the system clock at boot against the newest persisted log timestamp;
  if it booted >60 s behind, it logs `system.clock_rolled_back` (RTC/NTP then
  corrects the clock). Reuses existing log rows — no extra SD writes.
- **Turner scheduler:** `turn_due()` now clamps a backward jump — if
  `now_ts < last_turn_ts` it treats a turn as due rather than waiting out a
  bogus interval, so eggs never miss a turn after a clock reset.

### BOM — Part 2

| Item | Example part | Spec | ~Qty | ~Cost |
|------|--------------|------|------|-------|
| RTC module | DS3231 (ZS-042 or Adafruit PiRTC) | I²C 0x68, TCXO | 1 | $1–4 |
| Backup cell | CR2032 (or LIR2032 if charge circuit kept) | 3 V coin | 1 | $0.50 |

Total added cost: **~$2–5**.

### Validation procedure

1. Set the clock, power the Pi off for ≥24 h with only the coin-cell, power on
   with Wi-Fi **disabled**; confirm `date` reads correct time (RTC holdover).
2. Confirm `/dev/rtc0` exists and `timedatectl` shows the RTC as the source.
3. Roll the system clock backward by an hour and confirm the turner still fires
   on schedule and `system.clock_rolled_back` is logged.

---

## Summary of what ships where

| Redundancy | Physical BOM | Firmware / image change | Est. added cost |
|------------|-------------|-------------------------|-----------------|
| Independent thermal cutoff | 2-layer bimetal + TCO in heater leg | *(optional)* INA219 current-fault alarm | ~$3 (+~$3 diag) |
| Battery-backed RTC | DS3231 + coin-cell on free I²C bus | config.txt overlay, disable fake-hwclock, clock-sanity guards, gate check | ~$2–5 |

The BOM items are the hardware team's call. The **RTC OS enablement + clock
sanity guards** and the **thermal-cutoff diagnostics hook** are firmware changes
I can implement and fold into the next ship image on request — neither touches
the UI or the existing control/safety behavior.
