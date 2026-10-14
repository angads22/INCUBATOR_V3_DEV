# Build Quickstart — flash the card, wire the parts, hatch eggs

The promise of this document: **no code, no config files, no terminal.**
Flash the latest `img-*` release to a microSD card, wire the parts below to
the Pi's header, power it on, and follow the setup network on your phone.

## 1. Parts (the ≤$40-class build)

| # | Part | Notes |
|---|------|-------|
| 1 | Raspberry Pi Zero 2 W | with pre-soldered header ("WH") saves soldering |
| 2 | microSD card, 16–32 GB (Class 10/A1) | the whole OS + app lives here |
| 3 | Temp/humidity sensor: **DHT22 / AM2302 module** (3-pin, on a small PCB) **or** an I²C sensor module (SHT31, AHT20, BME280) | the DHT PCB version has the required pull-up resistor built in — buy this, not the bare 4-pin sensor. I²C modules are plug-and-play on the same two pins as the RTC |
| 4 | 2-channel 5 V relay module (opto-isolated, **active-LOW** trigger) | matches the firmware default; ch1 = heater, ch2 = fan |
| 5 | 12 V PTC heating element (20–50 W, with fan shroud if possible) | the heat source |
| 6 | 12 V ≥3 A power adapter (barrel jack) | powers heater + everything |
| 7 | Buck converter 12 V→5 V ≥3 A (MP1584/LM2596 board) | powers the Pi from the same adapter |
| 8 | Small 5 V/3 V active buzzer + momentary push button | alarm/beeps + setup button |
| 9 | Dupont jumper wires (F–F), a styrofoam/cooler box | wiring + the chamber |

Optional, plug-and-play when added later: Pi Camera (candling/vision, CSI
ribbon), DS3231 RTC module (clock survives power cuts, I²C), INA219 current
sensor (heater-fault alarm, same I²C bus). The device works without all three.

## 2. Wiring (BCM numbering — the labels silk-screened on most breakouts)

```
 DHT22 module   VCC → 3V3 (pin 1)   DATA → GPIO4 (pin 7)    GND → GND (pin 9)
   — or —
 I²C sensor     VCC → 3V3 (pin 1)   SDA → GPIO2 (pin 3)  SCL → GPIO3 (pin 5)  GND → GND
 Relay module   VCC → 5V  (pin 2)   IN1  → GPIO17 (pin 11)  GND → GND (pin 6)
   (heater)                          IN2  → GPIO27 (pin 13)  [fan, optional]
 Buzzer (+)     → GPIO25 (pin 22)   (–) → GND
 Setup button   one leg → GPIO18 (pin 12), other leg → GND
 Egg turner     STEP → GPIO22 (pin 15)  DIR → GPIO23 (pin 16)   [optional]
 Candling LED   → GPIO24 (pin 18) via 220 Ω                      [optional]
 DS3231 RTC     VCC → 3V3, GND → GND, SDA → GPIO2 (pin 3), SCL → GPIO3 (pin 5)
```

Power: the 12 V adapter feeds (a) the PTC heater **through relay ch1's
COM/NO contacts**, and (b) the buck converter, whose 5 V output goes to the
Pi's 5V + GND pins. One wall adapter runs everything.

Why active-LOW relays and nothing else: the shipped image drives the relay
pins **from power-on** (firmware `gpio=` directives put them in the OFF
state before the kernel even loads), the app initializes them OFF, the
control daemon fails safe (heater OFF) on any sensor problem, and the
image's zero-tolerance gate verifies all of it on every build.

> **Not negotiable for a real hatch:** add the ~$3 thermal cutoff (bimetal
> limit + thermal fuse) in series with the heater — see
> `docs/HARDWARE_SAFETY_ADDENDUM.md`. Software cannot open a welded relay.

## 3. First boot (what the farmer sees)

1. Flash the release `.img.xz` with Raspberry Pi Imager ("Use custom") and
   insert the card. Power on; give it ~2 minutes on first boot (the
   filesystem expands itself). The green light blinks fast while it starts;
   **one short beep** = ready.
2. **Two short beeps** and a double-blink: the incubator is broadcasting its
   own open Wi-Fi **Incubator-XXXX**. On a phone, join it. A setup page pops
   up; tap **Open setup in my browser** (or open `http://10.42.0.1:8000`).
3. **The wizard has two steps.** Step 1: pick your home Wi-Fi (+ password).
   Step 2: name the incubator and, if you like, create a login. Tap
   **Connect**.
4. The light blinks fast while it joins your Wi-Fi, then plays a rising
   **short-short-long** chime and settles into a slow heartbeat: it is on
   your network. The setup page **finds the incubator by itself** and shows
   **Open my incubator** — tap it and add the page to your home screen.
   Bookmark address: `http://<name>.local:8000` (the IP is shown too, for
   Android phones).
5. If the Wi-Fi password was wrong: **one long buzz** and an S-O-S blink, the
   Incubator-XXXX network **comes back by itself** and the wizard reopens
   asking only for the Wi-Fi again. Nothing else is lost — this is by
   design, not a failure.

### What the light and the beeper mean

| Event | Buzzer | Status LED |
|---|---|---|
| Power on, app ready | 1 short | fast blink → then the pattern below |
| Setup mode (own Wi-Fi up) | 2 short | double-blink (pairing) |
| Joining your Wi-Fi | – | fast blink |
| Connected | short-short-long (rising) | slow heartbeat |
| Couldn't join / offline | 1 long | SOS |
| Setup button registered (4 s) | 1 medium | – |
| Factory-reset armed (15 s) | 3 rapid, then 1 long on release | fast blink |

### The setup button

* **Hold 4 s** (one medium beep): setup mode again — the Incubator-XXXX Wi-Fi
  comes back so you can move the incubator to a new network. Also in the app
  under the profile menu → *Setup / change Wi-Fi*.
* **Hold 15 s** (three rapid beeps): factory reset is armed; **let go** to
  confirm (one long beep). Accounts, Wi-Fi and settings are wiped and the
  device returns to setup mode. Release before the three beeps to abort.

From then on: the thermostat holds 37.5 °C automatically, eggs turn every
3 h, alerts buzz + show on the dashboard, and the app updates itself over
the air. No terminal, ever.
