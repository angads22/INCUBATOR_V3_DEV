#!/usr/bin/env bash
# Verify what install.sh left on a system (used by CI's install dry-run and
# handy after a manual install):  sudo scripts/verify_install.sh [install_dir]
# Exits non-zero if anything the appliance depends on is missing.
set -uo pipefail

INSTALL_DIR="${1:-/opt/incubator}"
ok=1
pass() { echo "  [ok]   $*"; }
fail() { echo "  [FAIL] $*" >&2; ok=0; }
check_file() { [[ -f "$1" ]] && pass "$2" || fail "$2 — missing $1"; }
check_link() { [[ -L "$1" || -e "$1" ]] && pass "$2" || fail "$2 — missing $1"; }

echo "Verifying Incubator install at ${INSTALL_DIR}"

# App + venv
check_file "${INSTALL_DIR}/app/version.py"            "app staged"
[[ -x "${INSTALL_DIR}/.venv/bin/python" ]] && pass "venv present" || fail "venv missing"
"${INSTALL_DIR}/.venv/bin/python" -c 'import uvicorn' 2>/dev/null && pass "uvicorn importable from the venv" || fail "uvicorn not importable"
[[ -d "${INSTALL_DIR}/.git" ]] && pass "git checkout for OTA" || fail "${INSTALL_DIR}/.git missing"
check_file "${INSTALL_DIR}/.git-ref"                  ".git-ref written"
"${INSTALL_DIR}/.venv/bin/python" -c 'import app.main' 2>/dev/null && pass "app imports" || fail "app.main does not import"

# Env file
check_file /etc/incubator.env "env file"
if [[ -f /etc/incubator.env ]]; then
    grep -q '^INCUBATOR_AP_PASSWORD=$' /etc/incubator.env      && pass "setup AP open"        || fail "INCUBATOR_AP_PASSWORD not blank"
    grep -q '^INCUBATOR_WIFI_COUNTRY=[A-Z][A-Z]$' /etc/incubator.env && pass "Wi-Fi country set" || fail "INCUBATOR_WIFI_COUNTRY missing"
    grep -q '^CONTROL_DAEMON_ENABLED=true' /etc/incubator.env  && pass "control daemon on"     || fail "CONTROL_DAEMON_ENABLED != true"
    for k in GPIO_BACKEND SENSOR_BACKEND CAMERA_BACKEND STATUS_LED; do
        grep -q "^${k}=" /etc/incubator.env && pass "env has ${k}" || fail "env lacks ${k}"
    done
fi

# Units: present, templated, enabled
for u in incubator.service incubator-control.service incubator-ota.service incubator-firstboot.service incubator-wifi-country.service; do
    check_file "/etc/systemd/system/$u" "unit $u installed"
    [[ -f "/etc/systemd/system/$u" ]] && grep -q '__INSTALL_DIR__\|__DATA_DIR__' "/etc/systemd/system/$u" && fail "unit $u still has a template placeholder"
done
check_file /etc/systemd/system/incubator-ota.timer "OTA timer unit"
for u in incubator.service incubator-control.service incubator-firstboot.service incubator-wifi-country.service NetworkManager.service; do
    check_link "/etc/systemd/system/multi-user.target.wants/$u" "$u enabled"
done
check_link /etc/systemd/system/timers.target.wants/incubator-ota.timer "OTA timer enabled"
grep -q "ExecStart=${INSTALL_DIR}/.venv/bin/python -m uvicorn" /etc/systemd/system/incubator.service 2>/dev/null \
    && pass "incubator.service points at the venv" || fail "incubator.service ExecStart does not point at ${INSTALL_DIR}"
grep -q "SupplementaryGroups" /etc/systemd/system/incubator*.service 2>/dev/null \
    && fail "units still use SupplementaryGroups" || pass "no SupplementaryGroups (root-only units)"

# Networking stack
check_file /etc/NetworkManager/conf.d/10-incubator.conf "NetworkManager drop-in"
check_file /etc/NetworkManager/dnsmasq-shared.d/090-incubator-captive.conf "captive DNS conf"
check_file /etc/avahi/services/incubator.service "avahi service advertisement"
check_file /etc/tmpfiles.d/incubator.conf "tmpfiles runtime dir"
command -v nmcli >/dev/null 2>&1   && pass "nmcli present"   || fail "nmcli missing"
command -v dnsmasq >/dev/null 2>&1 && pass "dnsmasq present" || fail "dnsmasq missing (hotspot DHCP)"
[[ -e /usr/sbin/avahi-daemon || -e /usr/bin/avahi-daemon ]] && pass "avahi-daemon present" || fail "avahi-daemon missing"

# Identity must not be pre-burned (image builds)
[[ ! -e /etc/incubator-firstboot.done ]] && pass "firstboot not pre-burned" || fail "/etc/incubator-firstboot.done exists"

if [[ "$ok" == "1" ]]; then echo "ALL CHECKS PASSED"; exit 0; fi
echo "SOME CHECKS FAILED" >&2; exit 1
