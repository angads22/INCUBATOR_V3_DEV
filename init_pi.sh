#!/usr/bin/env bash
# ============================================================
#  Incubator v3 — legacy entry point (Raspberry Pi).
#
#  Kept for compatibility with older docs/scripts:
#      sudo bash init_pi.sh [install_dir]
#  Everything now lives in the board-agnostic install.sh, which does
#  the same on a Pi (and on any other Linux SBC):
#    * packages, NetworkManager-owned Wi-Fi, mDNS, venv, /etc/incubator.env
#    * systemd units, hardware watchdog, OTA git checkout
#    * on a Pi: I2C + DS3231 RTC overlay and the boot-safe GPIO levels
#      (gpio=12,13,17,27=op,dh / gpio=22,23,24,25=op,dl — relays OFF from
#      power-on), zram swap, Bluetooth/idle timers off.
#  Honours INCUBATOR_IMAGE_BUILD=1 exactly as before (enable units only).
# ============================================================
exec "$(dirname "$0")/install.sh" "$@"
