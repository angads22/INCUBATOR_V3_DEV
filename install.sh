#!/usr/bin/env bash
# =============================================================================
#  install.sh — Incubator v3 installer for ANY Linux single-board computer.
#
#  Turns a stock Linux install (Raspberry Pi OS, Debian, Ubuntu, Armbian,
#  Fedora, Arch, openSUSE, Alpine…) into the incubator appliance: system
#  packages, NetworkManager-owned Wi-Fi (for the setup hotspot), mDNS, the
#  Python venv, /etc/incubator.env, the systemd units, the hardware watchdog,
#  SD-card-friendly tuning and the git checkout the OTA updater updates from.
#
#  Reference board: Raspberry Pi Zero 2 W. Pi-specific steps (config.txt
#  overlays, boot-safe GPIO levels, zram, Bluetooth off) run only on a Pi.
#
#  Usage (on the device, as root):
#    sudo bash install.sh [install_dir] [options]
#  or, with nothing checked out yet:
#    curl -fsSL https://raw.githubusercontent.com/angads22/INCUBATOR_V3_DEV/main/install.sh | sudo bash
#
#  Options:
#    [install_dir]          Positional install dir (compat; default /opt/incubator).
#    --install-dir <dir>    Same as the positional argument.
#    --data-dir <dir>       Persistent data dir for captures/models (default /var/incubator).
#    --wifi-country <CC>    Wi-Fi regulatory country, ISO 3166-1 alpha-2 (default:
#                           $INCUBATOR_WIFI_COUNTRY or US). The radio stays
#                           rfkill-blocked on many kernels until this is set.
#    --ref <git ref>        Branch/tag to clone when bootstrapping outside a
#                           checkout (default main).
#    --no-tuning            Skip the performance tuning (journald cap, noatime,
#                           zram, Bluetooth/timers off, boot config cosmetics).
#                           Safety-relevant boot config (relays OFF from power-on,
#                           I2C/RTC) is still applied on a Pi.
#    --skip-packages        Do not touch the package manager (air-gapped re-run).
#    -h, --help             Show this help.
#
#  Environment:
#    INCUBATOR_IMAGE_BUILD=1   Set by build_image.sh / rpi-image-gen when running
#                              inside an image chroot: units are only ENABLED
#                              (symlinks), nothing is started or health-checked.
#    INCUBATOR_IS_PI=1|0       Force board detection (used inside image chroots,
#                              where /proc/device-tree belongs to the build host).
#    INCUBATOR_WIFI_COUNTRY    Default for --wifi-country.
#    INCUBATOR_AP_IP           Setup-hotspot IP for the captive DNS (10.42.0.1).
#
#  Idempotent: safe to re-run on an installed device. /etc/incubator.env is
#  preserved (new keys are appended with their defaults), units are re-templated.
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/angads22/INCUBATOR_V3_DEV"
SERVICE_NAME="incubator"
ENV_FILE="/etc/${SERVICE_NAME}.env"
INSTALL_DIR="/opt/incubator"
DATA_DIR="/var/incubator"
GIT_REF="main"
DO_TUNING=1
IMAGE_BUILD="${INCUBATOR_IMAGE_BUILD:-0}"
SKIP_PACKAGES="${INCUBATOR_SKIP_PACKAGES:-0}"
WIFI_COUNTRY="${INCUBATOR_WIFI_COUNTRY:-US}"
# Only an explicit --wifi-country / INCUBATOR_WIFI_COUNTRY overwrites the value
# already in /etc/incubator.env on a re-run.
WIFI_COUNTRY_EXPLICIT=0; [[ -n "${INCUBATOR_WIFI_COUNTRY:-}" ]] && WIFI_COUNTRY_EXPLICIT=1
AP_IP="${INCUBATOR_AP_IP:-10.42.0.1}"
ORIG_ARGS=("$@")

# ── Colour helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
step()  { echo -e "${BLUE}[STEP]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

usage() { sed -n '3,/^# ===/p' "${BASH_SOURCE[0]:-$0}" | sed '$d; s/^# \{0,1\}//'; exit 0; }

# ── Parse args ───────────────────────────────────────────────────────────────
POSITIONAL_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install-dir)   INSTALL_DIR="${2:-}"; shift 2 ;;
        --data-dir)      DATA_DIR="${2:-}"; shift 2 ;;
        --wifi-country)  WIFI_COUNTRY="${2:-}"; WIFI_COUNTRY_EXPLICIT=1; shift 2 ;;
        --ref)           GIT_REF="${2:-}"; shift 2 ;;
        --no-tuning)     DO_TUNING=0; shift ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        -h|--help)       usage ;;
        --*)             error "Unknown option: $1 (try --help)" ;;
        *)  [[ -z "$POSITIONAL_DIR" ]] || error "Unexpected argument: $1"
            POSITIONAL_DIR="$1"; shift ;;
    esac
done
[[ -n "$POSITIONAL_DIR" ]] && INSTALL_DIR="$POSITIONAL_DIR"
[[ -n "$INSTALL_DIR" && -n "$DATA_DIR" ]] || error "--install-dir / --data-dir must not be empty."
INSTALL_DIR="${INSTALL_DIR%/}"; DATA_DIR="${DATA_DIR%/}"
VENV_DIR="${INSTALL_DIR}/.venv"

[[ "$WIFI_COUNTRY" =~ ^[A-Za-z]{2}$ ]] || { warn "Invalid Wi-Fi country '${WIFI_COUNTRY}' — using US."; WIFI_COUNTRY="US"; }
WIFI_COUNTRY="${WIFI_COUNTRY^^}"

[[ "${EUID:-$(id -u)}" -eq 0 ]] || error "Run as root: sudo bash ${BASH_SOURCE[0]:-install.sh}"
[[ "$(uname -s)" == "Linux" ]] || error "This installer only runs on Linux."

# ── Detect OS / package manager / board ──────────────────────────────────────
OS_ID="$(. /etc/os-release 2>/dev/null; echo "${ID:-unknown}")"
OS_LIKE="$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}")"
OS_VERSION="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-}")"

PKG="none"
for _pm in apt-get dnf yum pacman zypper apk; do
    if command -v "$_pm" >/dev/null 2>&1; then PKG="$_pm"; break; fi
done
[[ "$PKG" == "yum" ]] && PKG="dnf"

ARCH="$(uname -m)"
if command -v dpkg >/dev/null 2>&1; then
    # Inside an image chroot the kernel is the build host's; trust the userland.
    case "$(dpkg --print-architecture 2>/dev/null || true)" in
        arm64) ARCH="aarch64" ;; armhf) ARCH="armv7l" ;; armel) ARCH="armv6l" ;; amd64) ARCH="x86_64" ;;
    esac
fi

BOARD_MODEL="Generic Linux"
for _m in /proc/device-tree/model /sys/firmware/devicetree/base/model; do
    if [[ -r "$_m" ]]; then BOARD_MODEL="$(tr -d '\0' < "$_m" 2>/dev/null || true)"; break; fi
done
[[ -n "$BOARD_MODEL" ]] || BOARD_MODEL="Generic Linux"

IS_ARMBIAN=0; [[ -f /etc/armbian-release ]] && IS_ARMBIAN=1

BOOT_CONFIG=""
for _cfg in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$_cfg" ]] && { BOOT_CONFIG="$_cfg"; break; }
done

IS_PI=0
if [[ -n "${INCUBATOR_IS_PI:-}" ]]; then
    [[ "${INCUBATOR_IS_PI}" == "1" ]] && IS_PI=1
elif [[ "$BOARD_MODEL" == *"Raspberry Pi"* || -f /etc/rpi-issue || "$OS_ID" == "raspbian" ]]; then
    IS_PI=1
elif [[ -n "$BOOT_CONFIG" ]] && grep -qE '^\s*(dtparam=|dtoverlay=|\[pi|\[all\]|arm_64bit|enable_uart)' "$BOOT_CONFIG" 2>/dev/null; then
    IS_PI=1   # a Pi-style config.txt (image chroot: /proc/device-tree is the host's)
fi
if [[ "$IS_PI" == "0" && "$IS_ARMBIAN" == "1" && -f /boot/armbianEnv.txt ]]; then
    BOOT_CONFIG="/boot/armbianEnv.txt"
fi

HAVE_SYSTEMCTL=0; command -v systemctl >/dev/null 2>&1 && HAVE_SYSTEMCTL=1
SYSTEMD_RUNNING=0
[[ -d /run/systemd/system && "$IMAGE_BUILD" != "1" ]] && SYSTEMD_RUNNING=1

is_debian_family() { [[ "$OS_ID" =~ ^(debian|raspbian|ubuntu)$ || "$OS_LIKE" == *debian* || "$OS_LIKE" == *ubuntu* ]]; }

# ── Bootstrap: not running from a checkout → clone and re-exec ───────────────
SCRIPT_SRC="${BASH_SOURCE[0]:-}"
REPO_DIR=""
if [[ -n "$SCRIPT_SRC" && -f "$SCRIPT_SRC" ]]; then
    REPO_DIR="$(cd "$(dirname "$SCRIPT_SRC")" && pwd)"
fi
if [[ -z "$REPO_DIR" || ! -f "${REPO_DIR}/pyproject.toml" ]]; then
    step "Bootstrapping: fetching Incubator v3 (${GIT_REF}) from ${REPO_URL}..."
    if ! command -v git >/dev/null 2>&1; then
        case "$PKG" in
            apt-get) apt-get update -qq || true; apt-get install -y --no-install-recommends git ca-certificates ;;
            dnf)     dnf install -y git ca-certificates ;;
            pacman)  pacman -Sy --needed --noconfirm git ca-certificates ;;
            zypper)  zypper -n in git ca-certificates ;;
            apk)     apk add git ca-certificates ;;
            *)       error "git is required to bootstrap; install it and re-run." ;;
        esac
    fi
    BOOT_TMP="$(mktemp -d /tmp/incubator-bootstrap.XXXXXX)"
    git clone --depth 1 --branch "$GIT_REF" "${REPO_URL}" "${BOOT_TMP}/src" \
        || error "git clone of ${REPO_URL} (${GIT_REF}) failed."
    exec bash "${BOOT_TMP}/src/install.sh" ${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}
fi

info "Incubator v3 installer"
info "  Board:        ${BOARD_MODEL} (${ARCH})$( [[ "$IS_PI" == "1" ]] && echo ' [Raspberry Pi]')$( [[ "$IS_ARMBIAN" == "1" ]] && echo ' [Armbian]')"
info "  OS:           ${OS_ID} ${OS_VERSION} (pkg: ${PKG})"
info "  Install dir:  ${INSTALL_DIR}"
info "  Data dir:     ${DATA_DIR}"
info "  Repo dir:     ${REPO_DIR}"
info "  Boot config:  ${BOOT_CONFIG:-none}"
info "  Wi-Fi country ${WIFI_COUNTRY}"
[[ "$IMAGE_BUILD" == "1" ]] && info "  Mode:         IMAGE BUILD (enable units only; nothing started)"
echo ""

# ── systemd helpers (work with a running manager, in a chroot, or without one) ─
unit_path() {
    local u="$1" d
    for d in /etc/systemd/system /usr/lib/systemd/system /lib/systemd/system; do
        [[ -f "$d/$u" ]] && { echo "$d/$u"; return 0; }
    done
    return 1
}
unit_present() {
    local u="$1"
    [[ "$HAVE_SYSTEMCTL" == "1" ]] || return 1
    systemctl list-unit-files "$u" 2>/dev/null | grep -qE "^${u//./\\.}[[:space:]]" && return 0
    unit_path "$u" >/dev/null 2>&1
}
# Enable a unit. `systemctl enable` works offline (chroot) too, but fall back to
# — and always verify — the wants-symlink so enablement is deterministic (the
# image verification gate asserts the symlink exists).
enable_unit() {
    local u="$1" target="multi-user.target" path
    [[ "$HAVE_SYSTEMCTL" == "1" ]] || { warn "systemctl not found — enable ${u} with your init system."; return 0; }
    [[ "$u" == *.timer ]] && target="timers.target"
    systemctl enable "$u" 2>/dev/null || true
    if [[ ! -e "/etc/systemd/system/${target}.wants/${u}" ]]; then
        path="$(unit_path "$u" || true)"
        if [[ -n "$path" ]]; then
            mkdir -p "/etc/systemd/system/${target}.wants"
            ln -sf "$path" "/etc/systemd/system/${target}.wants/${u}"
        else
            warn "Unit ${u} not found on disk — cannot enable."
        fi
    fi
}
start_unit() {   # only when a manager is running (never in image build)
    [[ "$SYSTEMD_RUNNING" == "1" ]] || return 0
    systemctl start "$1" 2>/dev/null || true
}
disable_unit_if_present() {
    local u="$1"
    unit_present "$u" || return 0
    systemctl disable "$u" 2>/dev/null || true
    if [[ "$SYSTEMD_RUNNING" == "1" ]]; then systemctl stop "$u" 2>/dev/null || true; fi
    rm -f "/etc/systemd/system/multi-user.target.wants/${u}" "/etc/systemd/system/timers.target.wants/${u}" 2>/dev/null || true
    info "  disabled ${u}"
}
mask_unit_if_present() {
    local u="$1"
    unit_present "$u" || return 0
    disable_unit_if_present "$u"
    systemctl mask "$u" 2>/dev/null || ln -sf /dev/null "/etc/systemd/system/${u}" 2>/dev/null || true
}

# ── Package installation ─────────────────────────────────────────────────────
pkg_install() {   # pkg_install <pkg...> ; returns the manager's exit code
    [[ $# -gt 0 ]] || return 0
    case "$PKG" in
        apt-get) DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" ;;
        dnf)     dnf install -y "$@" ;;
        pacman)  pacman -S --needed --noconfirm "$@" ;;
        zypper)  zypper -n in "$@" ;;
        apk)     apk add "$@" ;;
        *)       return 1 ;;
    esac
}
# Try the whole set at once; on failure fall back to one-by-one so a single
# unavailable package (e.g. rfkill living inside util-linux on Fedora) never
# blocks the rest. tier=fatal → any failure aborts; tier=warn → just report.
pkg_install_tier() {
    local tier="$1"; shift
    [[ $# -gt 0 ]] || return 0
    if pkg_install "$@" >/dev/null 2>&1; then return 0; fi
    local p failed=()
    for p in "$@"; do
        pkg_install "$p" >/dev/null 2>&1 || failed+=("$p")
    done
    if ((${#failed[@]})); then
        if [[ "$tier" == "fatal" ]]; then
            error "Failed to install essential packages: ${failed[*]}"
        else
            warn "Optional packages not installed (continuing): ${failed[*]}"
        fi
    fi
}

install_packages() {
    if [[ "$SKIP_PACKAGES" == "1" ]]; then
        warn "Skipping system packages (--skip-packages)."
        return 0
    fi
    step "Installing system packages (${PKG})..."
    local essential=() recommended=() pi_extra=()
    case "$PKG" in
        apt-get)
            # apt-get can transiently fail under cross-arch emulation (image builds); retry.
            apt-get update -qq \
                || { warn "apt-get update failed; retrying in 3s..."; sleep 3; apt-get update -qq; } \
                || error "apt-get update failed."
            # The libgpiod runtime package name varies by Debian base (t64 transition).
            local gpio_rt="" pkgname
            for pkgname in libgpiod2 libgpiod2t64 libgpiod3; do
                if apt-cache show "$pkgname" >/dev/null 2>&1; then gpio_rt="$pkgname"; break; fi
            done
            [[ -n "$gpio_rt" ]] || warn "No libgpiod runtime package found — pip's gpiod wheel will be used."
            # wpasupplicant + dnsmasq-base are only Recommends of network-manager; with
            # --no-install-recommends they would be skipped and Wi-Fi/the hotspot would
            # not work at all on plain Debian/Ubuntu.
            essential=(python3 python3-venv python3-pip python3-dev git curl ca-certificates
                       network-manager wpasupplicant dnsmasq-base)
            recommended=(rfkill iw wireless-regdb avahi-daemon python3-libgpiod i2c-tools
                         gcc build-essential libjpeg-dev zlib1g-dev rsync)
            [[ -n "$gpio_rt" ]] && recommended+=("$gpio_rt")
            # Camera + BLAS are heavier and only used for vision/candling. Their
            # maintainer scripts can choke under QEMU emulation, so never let
            # them abort an install: sudo apt install python3-picamera2 later.
            pi_extra=(python3-picamera2 libatlas-base-dev)
            ;;
        dnf)
            essential=(python3 python3-pip python3-devel git curl ca-certificates NetworkManager)
            recommended=(dnsmasq NetworkManager-wifi wpa_supplicant rfkill iw wireless-regdb avahi libgpiod
                         python3-libgpiod i2c-tools gcc libjpeg-turbo-devel zlib-devel rsync)
            ;;
        pacman)
            pacman -Sy --noconfirm >/dev/null 2>&1 || warn "pacman -Sy failed (continuing)."
            essential=(python python-pip git curl ca-certificates networkmanager)
            recommended=(dnsmasq wpa_supplicant iw wireless-regdb avahi libgpiod i2c-tools gcc libjpeg-turbo zlib rsync)
            ;;
        zypper)
            essential=(python3 python3-pip python3-devel git curl ca-certificates NetworkManager)
            recommended=(dnsmasq wpa_supplicant rfkill iw wireless-regdb avahi libgpiod3 python3-libgpiod
                         i2c-tools gcc libjpeg8-devel zlib-devel rsync)
            ;;
        apk)
            essential=(python3 py3-pip python3-dev git curl ca-certificates networkmanager
                       networkmanager-wifi networkmanager-cli)
            recommended=(dnsmasq wpa_supplicant rfkill iw wireless-regdb avahi libgpiod py3-libgpiod i2c-tools
                         gcc musl-dev libjpeg-turbo-dev zlib-dev rsync)
            ;;
        *)
            warn "Unknown package manager. Install these yourself, then re-run with --skip-packages:"
            warn "  python3 (+venv, pip, headers) git curl ca-certificates NetworkManager dnsmasq"
            warn "  rfkill iw wireless-regdb avahi libgpiod python3-libgpiod i2c-tools gcc libjpeg zlib"
            return 0
            ;;
    esac
    pkg_install_tier fatal "${essential[@]}"
    pkg_install_tier warn ${recommended[@]+"${recommended[@]}"}
    if [[ "$IS_PI" == "1" ]] && ((${#pi_extra[@]})); then
        pkg_install_tier warn "${pi_extra[@]}"
    fi
    if [[ "$PKG" == "apt-get" ]]; then apt-get clean >/dev/null 2>&1 || true; fi

    # dnsmasq is what NetworkManager's shared/AP mode uses for DHCP+DNS on the
    # setup hotspot. Without it the hotspot comes up but phones get no address.
    command -v dnsmasq >/dev/null 2>&1 \
        || warn "dnsmasq binary not found — the setup hotspot cannot hand out IP addresses. Install dnsmasq(-base)."
    command -v nmcli >/dev/null 2>&1 \
        || warn "nmcli not found — Wi-Fi setup needs NetworkManager."
    python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
        || error "python3 cannot create virtual environments (install python3-venv / ensurepip)."
}

# ── NetworkManager owns Wi-Fi ────────────────────────────────────────────────
configure_networkmanager() {
    step "Configuring NetworkManager (owns Wi-Fi, hotspot-capable)..."
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/10-incubator.conf <<'EOF'
# Installed by the Incubator installer.
[main]
plugins=keyfile

[keyfile]
# Let NetworkManager manage every interface (Ubuntu ships a rule that hides
# wired/wireless devices from NM when netplan uses networkd).
unmanaged-devices=none

[connection]
# Wi-Fi power saving OFF: the Pi Zero 2 W drops the AP / goes deaf to mDNS
# with powersave on, which looks like "the incubator disappeared".
wifi.powersave=2
EOF

    # Ubuntu: hand networking to NetworkManager via netplan.
    if [[ -d /etc/netplan ]]; then
        cat > /etc/netplan/90-incubator-nm.yaml <<'EOF'
# Installed by the Incubator installer: NetworkManager must own Wi-Fi for the
# setup hotspot and the home-network join to work.
network:
  version: 2
  renderer: NetworkManager
EOF
        chmod 600 /etc/netplan/90-incubator-nm.yaml
        if [[ "$IMAGE_BUILD" != "1" ]] && command -v netplan >/dev/null 2>&1; then
            netplan generate >/dev/null 2>&1 || warn "netplan generate failed (continuing)."
        fi
    fi

    # Standalone Wi-Fi daemons fight NetworkManager for the radio. Disable the
    # ones that are actually enabled (best effort, only if present).
    local inst
    for inst in /etc/systemd/system/multi-user.target.wants/wpa_supplicant@*.service \
                /etc/systemd/system/multi-user.target.wants/wpa_supplicant-nl80211@*.service \
                /etc/systemd/system/multi-user.target.wants/wpa_supplicant-wired@*.service; do
        [[ -e "$inst" ]] || continue
        disable_unit_if_present "$(basename "$inst")"
    done
    disable_unit_if_present iwd.service
    disable_unit_if_present connman.service
    disable_unit_if_present dhcpcd.service   # legacy Raspberry Pi OS (Bullseye)

    enable_unit NetworkManager.service
    start_unit NetworkManager.service
    if unit_present avahi-daemon.service; then
        enable_unit avahi-daemon.service
        start_unit avahi-daemon.service
    else
        warn "avahi-daemon not installed — <hostname>.local will not resolve."
    fi
    return 0
}

configure_firewall() {
    if command -v firewall-cmd >/dev/null 2>&1; then
        step "Opening firewall ports (firewalld)..."
        local fw="firewall-cmd --permanent"
        if [[ "$SYSTEMD_RUNNING" != "1" ]] || ! firewall-cmd --state >/dev/null 2>&1; then
            command -v firewall-offline-cmd >/dev/null 2>&1 && fw="firewall-offline-cmd"
        fi
        $fw --add-port=8000/tcp --add-port=80/tcp --add-service=mdns --add-service=dhcp --add-service=dns \
            >/dev/null 2>&1 || warn "firewalld rule add failed (continuing)."
        if [[ "$fw" == "firewall-cmd --permanent" ]]; then firewall-cmd --reload >/dev/null 2>&1 || true; fi
    fi
    if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
        step "Opening firewall ports (ufw)..."
        local rule
        for rule in 8000/tcp 80/tcp 5353/udp 67/udp 53; do
            ufw allow "$rule" >/dev/null 2>&1 || warn "ufw allow ${rule} failed."
        done
    fi
    return 0
}

# ── Wi-Fi regulatory country (unblocks the radio) ────────────────────────────
configure_wifi_country() {
    step "Configuring Wi-Fi regulatory country: ${WIFI_COUNTRY}"
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint do_wifi_country "${WIFI_COUNTRY}" >/dev/null 2>&1 \
            || warn "raspi-config do_wifi_country failed (continuing)."
    fi
    # crda regdomain (best-effort; harmless where crda is absent).
    echo "REGDOMAIN=${WIFI_COUNTRY}" > /etc/default/crda 2>/dev/null || true

    # Oneshot unit: unblock WLAN + apply the regulatory domain before the radio
    # is used. Without a country many kernels keep the WLAN rfkill soft-blocked
    # and the onboarding hotspot never comes up.
    [[ "$HAVE_SYSTEMCTL" == "1" ]] || return 0
    cat > /etc/systemd/system/incubator-wifi-country.service <<EOF
[Unit]
Description=Incubator: unblock WLAN radio and set Wi-Fi regulatory domain
Before=NetworkManager.service ${SERVICE_NAME}.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'rfkill unblock wlan || true; iw reg set ${WIFI_COUNTRY} || true'

[Install]
WantedBy=multi-user.target
EOF
    enable_unit incubator-wifi-country.service
}

# ── Captive-portal DNS hijack (setup hotspot) ────────────────────────────────
configure_captive_dns() {
    # NetworkManager's shared (AP) mode runs its own dnsmasq. Resolving every
    # domain to the AP IP while the hotspot is up makes phones/laptops fire
    # their "sign in to network" sheet, which the app's :80 responder turns
    # into the onboarding page. Only the shared/AP connection is affected.
    step "Installing captive-portal DNS config for the setup hotspot..."
    mkdir -p /etc/NetworkManager/dnsmasq-shared.d
    cat > /etc/NetworkManager/dnsmasq-shared.d/090-incubator-captive.conf <<EOF
# Resolve all names to the incubator AP so the onboarding portal auto-opens.
address=/#/${AP_IP}
EOF
}

# ── Runtime dir + mDNS advertisement ─────────────────────────────────────────
configure_tmpfiles_avahi() {
    step "Installing runtime-dir (tmpfiles) and mDNS service advertisement..."
    mkdir -p /etc/tmpfiles.d
    install -m 0644 "${INSTALL_DIR}/deploy/tmpfiles-incubator.conf" /etc/tmpfiles.d/incubator.conf
    if [[ "$IMAGE_BUILD" != "1" ]] && command -v systemd-tmpfiles >/dev/null 2>&1; then
        systemd-tmpfiles --create /etc/tmpfiles.d/incubator.conf >/dev/null 2>&1 || true
    fi
    mkdir -p /run/incubator 2>/dev/null || true
    mkdir -p /etc/avahi/services
    install -m 0644 "${INSTALL_DIR}/deploy/avahi/incubator.service" /etc/avahi/services/incubator.service
}

# ── Stage the application ────────────────────────────────────────────────────
stage_app() {
    mkdir -p "${INSTALL_DIR}"
    if [[ "$(cd "${REPO_DIR}" && pwd -P)" == "$(cd "${INSTALL_DIR}" && pwd -P)" ]]; then
        info "Application already in place at ${INSTALL_DIR}."
    else
        step "Deploying application to ${INSTALL_DIR}..."
        if command -v rsync >/dev/null 2>&1; then
            # The excludes also PROTECT those paths on the receiver from --delete:
            # a re-run must never wipe the production SQLite database, its daily
            # snapshots or the control daemon's persisted state.
            rsync -a --delete \
                --exclude='.git' --exclude='.venv' --exclude='*.pyc' --exclude='__pycache__' \
                --exclude='captures' --exclude='dist' --exclude='.image-cache' \
                --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.db.corrupt-*' \
                --exclude='database/backups' --exclude='database/control-persist.json' \
                "${REPO_DIR}/" "${INSTALL_DIR}/"
        else
            # No rsync: copy everything except the excluded trees (no --delete).
            (cd "${REPO_DIR}" && tar --exclude='./.git' --exclude='./.venv' --exclude='*.pyc' \
                --exclude='__pycache__' --exclude='./captures' --exclude='./dist' \
                --exclude='./.image-cache' --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
                --exclude='./database/backups' --exclude='./database/control-persist.json' \
                -cf - .) | (cd "${INSTALL_DIR}" && tar -xf -)
        fi
    fi
    chmod +x "${INSTALL_DIR}"/scripts/*.sh "${INSTALL_DIR}/install.sh" 2>/dev/null || true

    step "Creating data directories..."
    mkdir -p "${DATA_DIR}/captures" "${DATA_DIR}/models/vision" "${DATA_DIR}/models/llm" "${INSTALL_DIR}/database"
}

# ── Python virtual environment ───────────────────────────────────────────────
PIP_INDEX_ARGS=()
install_python() {
    step "Creating Python virtual environment..."
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        python3 -m venv --system-site-packages "${VENV_DIR}" || error "python3 -m venv failed."
    fi
    local pip="${VENV_DIR}/bin/pip"
    "$pip" install --upgrade pip --quiet >/dev/null 2>&1 || warn "pip self-upgrade failed (continuing with the bundled pip)."

    # piwheels has prebuilt ARM wheels for Debian-family distros ONLY (its wheels
    # are built against Raspberry Pi OS). Elsewhere let pip build/download from PyPI.
    if is_debian_family && [[ "$ARCH" =~ ^(armv6l|armv7l|aarch64)$ ]]; then
        PIP_INDEX_ARGS=(--extra-index-url https://www.piwheels.org/simple)
    fi

    step "Installing the application (this can take a while on a small board)..."
    "$pip" install ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} -e "${INSTALL_DIR}" --quiet \
        || error "pip install of the application failed."
    # Hardware extras: libgpiod 2.x bindings + smbus2 for I2C sensors. Best
    # effort — a distro python3-libgpiod is visible through system-site-packages.
    "$pip" install ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} -e "${INSTALL_DIR}[hardware]" --quiet \
        || warn "Hardware extras (gpiod, smbus2) not installed — using distro packages if present."
    if [[ "$IS_PI" == "1" ]]; then
        "$pip" install ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} -e "${INSTALL_DIR}[pi]" --quiet \
            || warn "Pi legacy extras (RPi.GPIO, adafruit-dht) not installed — libgpiod/IIO backends are used."
    fi
    # TFLite runtime (optional — only for an on-device vision model).
    "$pip" install ${PIP_INDEX_ARGS[@]+"${PIP_INDEX_ARGS[@]}"} tflite-runtime numpy --quiet >/dev/null 2>&1 \
        || warn "TFLite runtime not available — vision will use the heuristic/API/mock backend."

    # Trim the pip download cache so a baked image stays as small as possible.
    "$pip" cache purge >/dev/null 2>&1 || true
    rm -rf /root/.cache/pip "${HOME:-/root}/.cache/pip" 2>/dev/null || true
}

# ── Environment file ─────────────────────────────────────────────────────────
ensure_env_key() {   # ensure_env_key KEY VALUE — append if the key is absent
    grep -qE "^${1}=" "${ENV_FILE}" || echo "${1}=${2}" >> "${ENV_FILE}"
}
set_env_key() {      # set_env_key KEY VALUE — replace or append
    if grep -qE "^${1}=" "${ENV_FILE}"; then
        sed -i "s|^${1}=.*|${1}=${2}|" "${ENV_FILE}"
    else
        echo "${1}=${2}" >> "${ENV_FILE}"
    fi
}
install_env_file() {
    if [[ ! -f "${ENV_FILE}" ]]; then
        step "Creating ${ENV_FILE}..."
        local tmp; tmp="$(mktemp)"
        cp "${INSTALL_DIR}/deploy/incubator.env.example" "$tmp"
        [[ "$DATA_DIR" != "/var/incubator" ]] && sed -i "s|/var/incubator|${DATA_DIR}|g" "$tmp"
        install -m 0600 "$tmp" "${ENV_FILE}"; rm -f "$tmp"
        # Open setup network — no Wi-Fi password. The operator joins
        # "Incubator-XXXX" and creates their account in the captive-portal wizard.
        sed -i "s|^INCUBATOR_AP_PASSWORD=.*|INCUBATOR_AP_PASSWORD=|" "${ENV_FILE}"
        info "Setup AP is open (no Wi-Fi password)."
    else
        info "${ENV_FILE} exists — keeping your settings (new keys appended with defaults)."
        chmod 600 "${ENV_FILE}" 2>/dev/null || true
    fi
    # Bake the Wi-Fi country so the running app applies the same regulatory
    # domain before starting the hotspot. A re-run without an explicit country
    # keeps whatever the operator already has in the file.
    if [[ "$WIFI_COUNTRY_EXPLICIT" == "1" ]]; then
        set_env_key INCUBATOR_WIFI_COUNTRY "${WIFI_COUNTRY}"
    else
        ensure_env_key INCUBATOR_WIFI_COUNTRY "${WIFI_COUNTRY}"
    fi
    # Hardware-abstraction defaults (all "auto" = detect at runtime).
    ensure_env_key GPIO_BACKEND auto
    ensure_env_key SENSOR_BACKEND auto
    ensure_env_key CAMERA_BACKEND auto
    ensure_env_key STATUS_LED auto
    ensure_env_key INCUBATOR_SOUND_FEEDBACK true
    ensure_env_key INCUBATOR_FACTORY_RESET_HOLD_SECONDS 15
}

# ── systemd units ────────────────────────────────────────────────────────────
install_units() {
    [[ "$HAVE_SYSTEMCTL" == "1" ]] || {
        warn "No systemd: units not installed. Run the app with ${VENV_DIR}/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000"
        return 0
    }
    step "Installing systemd units..."
    local u
    for u in incubator.service incubator-control.service incubator-ota.service incubator-firstboot.service incubator-gpio-safe.service; do
        [[ -f "${INSTALL_DIR}/deploy/${u}" ]] || { warn "deploy/${u} missing — skipped."; continue; }
        sed -e "s|__INSTALL_DIR__|${INSTALL_DIR}|g" -e "s|__DATA_DIR__|${DATA_DIR}|g" \
            "${INSTALL_DIR}/deploy/${u}" > "/etc/systemd/system/${u}"
    done
    [[ -f "${INSTALL_DIR}/deploy/incubator-ota.timer" ]] \
        && cp "${INSTALL_DIR}/deploy/incubator-ota.timer" /etc/systemd/system/incubator-ota.timer
    chmod +x "${INSTALL_DIR}/scripts/firstboot.sh" "${INSTALL_DIR}/scripts/ota-agent.sh" 2>/dev/null || true

    # incubator.service: the web app. incubator-firstboot: stable device ID +
    # hostname + root-fs expand, once, before the app. incubator-control: the
    # always-on control loop (idles until CONTROL_DAEMON_ENABLED=true).
    # incubator-ota.timer: GitHub Releases auto-update with rollback.
    # incubator-gpio-safe: drives every relay/actuator line to its OFF level as
    # early as systemd can, before the control daemon and the web app start. On
    # a Pi the firmware already does this from power-on (config.txt gpio= lines);
    # on every other board this unit is the only software-side boot-safe layer.
    enable_unit incubator.service
    enable_unit incubator-firstboot.service
    enable_unit incubator-control.service
    enable_unit incubator-gpio-safe.service
    enable_unit incubator-ota.timer
}

# ── Hardware watchdog ────────────────────────────────────────────────────────
configure_watchdog() {
    # Restart=always only covers process EXITS. A wedged kernel or a deadlocked
    # userspace needs the SoC hardware watchdog: systemd pets /dev/watchdog and
    # the chip reboots the board if the whole system stops responding. The
    # BCM2835 watchdog's maximum timeout is ~15 s, so stay under it.
    if [[ -e /dev/watchdog || "$IS_PI" == "1" ]]; then
        step "Enabling hardware watchdog (via systemd)..."
        mkdir -p /etc/systemd/system.conf.d
        cat > /etc/systemd/system.conf.d/10-incubator-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=14
RebootWatchdogSec=2min
EOF
    else
        info "No /dev/watchdog on this board — hardware watchdog not configured."
    fi
}

# ── Boot config: I2C/RTC + boot-safe GPIO levels (Pi) ────────────────────────
pi_boot_safety_lines() {
    [[ "$IS_PI" == "1" ]] || return 0
    if [[ -z "$BOOT_CONFIG" || "$BOOT_CONFIG" != *config.txt ]]; then
        warn "No config.txt found — skipping I2C/RTC overlay + boot-safe GPIO levels (add them manually)."
        return 0
    fi
    step "Boot config: I2C + DS3231 RTC overlay + relays OFF from power-on (${BOOT_CONFIG})..."
    # Appended lines must land in an [all] section: config.txt is sectioned
    # ([cm4], [pi5], …) and a trailing filter would otherwise swallow our lines.
    local last_section
    last_section="$(grep -oE '^\[[^]]+\]' "$BOOT_CONFIG" | tail -1 || true)"
    [[ -z "$last_section" || "$last_section" == "[all]" ]] || echo '[all]' >> "$BOOT_CONFIG"

    # I2C bus + DS3231 overlay: battery-backed RTC (day-count and turn schedule
    # survive a power cut without internet) and the optional INA219 heater-
    # current sensor live on this bus. No-ops when the hardware isn't fitted.
    # fake-hwclock is deliberately LEFT ENABLED as the fallback for units with
    # no RTC; a fitted DS3231 takes precedence via the kernel rtc class.
    grep -qE '^[[:space:]]*dtparam=i2c_arm=on' "$BOOT_CONFIG" || echo 'dtparam=i2c_arm=on' >> "$BOOT_CONFIG"
    grep -qE '^[[:space:]]*dtoverlay=i2c-rtc,ds3231' "$BOOT_CONFIG" || echo 'dtoverlay=i2c-rtc,ds3231' >> "$BOOT_CONFIG"

    # Safe GPIO states from POWER-ON, before the kernel even loads. Without
    # this the relay pins sit as inputs with pull-downs for the ~40 s boot (and
    # during any crash-loop); an active-LOW relay board reads that as ON — the
    # HEATER would blast until the app sets its pins. The GPU drives the pins
    # to their OFF levels before anything else runs:
    #   12,13,17,27 (lock/door/heater/fan relays, active-LOW) → output HIGH
    #   22,23,24,25 (turner step/dir, candle, buzzer)         → output LOW
    # GPIO4 (DHT22) and GPIO18 (setup button) stay untouched as inputs.
    grep -qE '^[[:space:]]*gpio=12,13,17,27=op,dh' "$BOOT_CONFIG" || echo 'gpio=12,13,17,27=op,dh' >> "$BOOT_CONFIG"
    grep -qE '^[[:space:]]*gpio=22,23,24,25=op,dl' "$BOOT_CONFIG" || echo 'gpio=22,23,24,25=op,dl' >> "$BOOT_CONFIG"
    info "  I2C/RTC overlay + boot-safe GPIO levels written."
}

# ── Tuning ───────────────────────────────────────────────────────────────────
tune_generic() {
    step "Generic tuning: journald cap, noatime, no wait-online stalls..."
    mkdir -p /etc/systemd/journald.conf.d
    cat > /etc/systemd/journald.conf.d/10-incubator.conf <<'EOF'
# Keep the journal small on SD cards; the app logs ~1 line/poll.
[Journal]
SystemMaxUse=48M
Compress=yes
MaxRetentionSec=1month
EOF
    # noatime on the root filesystem: every file read would otherwise write.
    if [[ -f /etc/fstab ]] && awk '$1 !~ /^#/ && $2=="/" {found=1; if ($4 ~ /noatime/) has=1} END {exit !(found && !has)}' /etc/fstab; then
        if sed -i -E '/^[^#][^[:space:]]*[[:space:]]+\/[[:space:]]/ s/^([^[:space:]]+[[:space:]]+\/[[:space:]]+[^[:space:]]+[[:space:]]+)([^[:space:]]+)/\1\2,noatime/' /etc/fstab; then
            info "  noatime added to the root mount in /etc/fstab"
        else
            warn "  could not edit /etc/fstab for noatime."
        fi
    fi
    # Nothing here needs the network at boot; these only delay the hotspot.
    disable_unit_if_present NetworkManager-wait-online.service
    disable_unit_if_present systemd-networkd-wait-online.service
}

pi_set_cfg() {   # pi_set_cfg <key> <value>: key=value line, replacing an existing key=anything
    if grep -qE "^[[:space:]]*${1}=" "$BOOT_CONFIG"; then
        sed -i -E "s|^[[:space:]]*${1}=.*|${1}=${2}|" "$BOOT_CONFIG"
    else
        echo "${1}=${2}" >> "$BOOT_CONFIG"
    fi
}
tune_pi() {
    step "Raspberry Pi tuning: boot config, zram swap, idle services off..."
    if [[ -n "$BOOT_CONFIG" && "$BOOT_CONFIG" == *config.txt ]]; then
        grep -qE '^[[:space:]]*dtoverlay=disable-bt' "$BOOT_CONFIG" || echo 'dtoverlay=disable-bt' >> "$BOOT_CONFIG"
        pi_set_cfg disable_splash 1
        pi_set_cfg boot_delay 0
        pi_set_cfg dtparam=audio off      # becomes dtparam=audio=off, replacing audio=on
        pi_set_cfg gpu_mem 32
        info "  config.txt: Bluetooth off, splash off, boot_delay=0, audio off, gpu_mem=32"
    fi
    # zram swap instead of a swapfile on the SD card (512 MB boards swap).
    if [[ "$PKG" == "apt-get" && "$SKIP_PACKAGES" != "1" ]]; then
        pkg_install_tier warn zram-tools
    fi
    if [[ -f /etc/default/zramswap ]] || command -v zramswap >/dev/null 2>&1; then
        touch /etc/default/zramswap
        if grep -qE '^#?ALGO=' /etc/default/zramswap; then sed -i -E 's|^#?ALGO=.*|ALGO=lz4|' /etc/default/zramswap; else echo 'ALGO=lz4' >> /etc/default/zramswap; fi
        if grep -qE '^#?PERCENT=' /etc/default/zramswap; then sed -i -E 's|^#?PERCENT=.*|PERCENT=50|' /etc/default/zramswap; else echo 'PERCENT=50' >> /etc/default/zramswap; fi
        enable_unit zramswap.service
        disable_unit_if_present dphys-swapfile.service
        info "  zram swap (lz4, 50 %) enabled; dphys-swapfile disabled"
    fi
    # Idle services that cost RAM/CPU/SD writes on a headless appliance.
    local u
    for u in bluetooth.service hciuart.service triggerhappy.service ModemManager.service \
             apt-daily.timer apt-daily-upgrade.timer man-db.timer e2scrub_all.timer; do
        mask_unit_if_present "$u"
    done
}

tune_armbian() {
    [[ "$IS_ARMBIAN" == "1" && -f /boot/armbianEnv.txt ]] || return 0
    step "Armbian: checking I2C overlay in /boot/armbianEnv.txt..."
    local ov="${INCUBATOR_ARMBIAN_I2C_OVERLAY:-i2c0}"
    if grep -qE '^overlays=' /boot/armbianEnv.txt; then
        if grep -E '^overlays=' /boot/armbianEnv.txt | grep -qE '(^|[ =])i2c[0-9]'; then
            info "  overlays= already enables an I2C bus."
        else
            sed -i -E "s|^overlays=(.*)$|overlays=\1 ${ov}|" /boot/armbianEnv.txt
            info "  added '${ov}' to overlays= (set INCUBATOR_ARMBIAN_I2C_OVERLAY for your SoC; check with armbian-config)."
        fi
    else
        echo "overlays=${ov}" >> /boot/armbianEnv.txt
        info "  added 'overlays=${ov}' (check the overlay name for your SoC with armbian-config)."
    fi
}

# ── Make the app a git checkout so OTA can update from GitHub ────────────────
# OTA applies updates by fetching/checking out release tags. A staged app has
# no .git, so initialize one pointed at the GitHub remote and record the baked
# version. .gitignore keeps .venv/data untracked, and OTA's force-checkout
# leaves untracked files in place.
init_ota_checkout() {
    local app_version
    app_version="$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${INSTALL_DIR}/app/version.py" 2>/dev/null || echo dev)"
    if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
        step "Initializing ${INSTALL_DIR} as a git checkout for OTA (version ${app_version})..."
        git -C "${INSTALL_DIR}" init -q
        git -C "${INSTALL_DIR}" add -A
        git -C "${INSTALL_DIR}" -c user.email=build@incubator -c user.name=incubator-build \
            commit -qm "Image base v${app_version}" || true
    fi
    git -C "${INSTALL_DIR}" remote add origin "${REPO_URL}.git" 2>/dev/null \
        || git -C "${INSTALL_DIR}" remote set-url origin "${REPO_URL}.git"
    echo "${app_version}" > "${INSTALL_DIR}/.git-ref"
}

# ── Final summary ────────────────────────────────────────────────────────────
print_feedback_table() {
    echo "  What the light and the beeper mean:"
    echo "    power on, app ready ........ 1 short beep,       LED fast blink"
    echo "    setup mode (own Wi-Fi up) .. 2 short beeps,      LED double-blink"
    echo "    joining your Wi-Fi ......... (silent)            LED fast blink"
    echo "    connected .................. short-short-LONG,   LED slow heartbeat"
    echo "    couldn't join / offline .... 1 long buzz,        LED S-O-S"
    echo "    setup button held 4 s ...... 1 medium beep"
    echo "    factory reset armed 15 s ... 3 rapid beeps, 1 long on release, LED fast blink"
}
print_summary() {
    local ip host; ip="$(hostname -I 2>/dev/null | awk '{print $1}')"; host="$(hostname 2>/dev/null || echo incubator)"
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Incubator v3 — setup complete on ${BOARD_MODEL}${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════${NC}"
    echo ""
    [[ -n "$ip" ]] && echo "  Dashboard now:  http://${ip}:8000   (http://${host}.local:8000)"
    echo ""
    echo "  First boot with no Wi-Fi configured: the device broadcasts the OPEN"
    echo "  network  Incubator-XXXX . Join it on your phone — the setup page opens"
    echo "  by itself (or go to http://${AP_IP}:8000). Two steps: your Wi-Fi, then a"
    echo "  name + optional login. The page then finds the incubator on your network."
    echo ""
    echo "  Setup button (GPIO ${GPIO_SETUP_BUTTON_PIN:-18}): hold 4 s = setup mode again,"
    echo "  hold 15 s = factory reset (3 rapid beeps, release to confirm)."
    echo ""
    print_feedback_table
    echo ""
    echo "  Logs:    journalctl -u ${SERVICE_NAME} -u ${SERVICE_NAME}-control -f"
    echo "  Config:  ${ENV_FILE}"
    echo "  Detect:  ${VENV_DIR}/bin/python -m app.hal"
    echo ""
}

# ── Run ──────────────────────────────────────────────────────────────────────
install_packages
stage_app
configure_networkmanager
configure_firewall
configure_wifi_country
configure_captive_dns
configure_tmpfiles_avahi
# Legacy camera interface toggle (pre-Bookworm Pi OS); harmless elsewhere.
if command -v raspi-config >/dev/null 2>&1; then raspi-config nonint do_camera 0 >/dev/null 2>&1 || true; fi
install_python
install_env_file
install_units
configure_watchdog
pi_boot_safety_lines
if [[ "$DO_TUNING" == "1" ]]; then
    tune_generic
    if [[ "$IS_PI" == "1" ]]; then tune_pi; fi
    tune_armbian
else
    info "Tuning skipped (--no-tuning)."
fi
init_ota_checkout

if [[ "$IMAGE_BUILD" == "1" ]]; then
    info "Image build complete — units enabled, will start on first boot."
    info "First boot broadcasts the open Wi-Fi 'Incubator-XXXX' at http://${AP_IP}:8000"
    exit 0
fi

if [[ "$SYSTEMD_RUNNING" != "1" ]]; then
    warn "systemd is not running here — units installed but not started."
    print_summary
    exit 0
fi

step "Starting services..."
systemctl daemon-reload
systemctl restart incubator-wifi-country.service 2>/dev/null || true
systemctl restart "${SERVICE_NAME}-control.service" 2>/dev/null || true
systemctl restart "${SERVICE_NAME}.service"

info "Waiting for the web app..."
healthy=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    if curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1; then healthy=1; break; fi
    sleep 2
done
if [[ "$healthy" == "1" ]] || systemctl is-active --quiet "${SERVICE_NAME}"; then
    info "Service is running."
    print_summary
else
    error "Service failed to start. Check: journalctl -u ${SERVICE_NAME} -n 50"
fi
