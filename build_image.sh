#!/usr/bin/env bash
# =============================================================================
#  build_image.sh — Build a ready-to-flash Incubator v3 SD-card image.
#
#  This single script turns a stock Linux SBC image — by default the official
#  Raspberry Pi OS Lite (64-bit) — into a self-contained "appliance" image with
#  the Incubator app, all of its dependencies, and the systemd services
#  PRE-INSTALLED. The result is a single .img(.xz) you flash to a microSD card
#  with Raspberry Pi Imager / balenaEtcher / dd. On first boot the board needs
#  NO internet: it starts the incubator service, broadcasts the open Wi-Fi
#  setup hotspot, and walks you through Wi-Fi + account creation in the browser.
#
#  How it works (no pi-gen required):
#    1. Download (or reuse) the base image.
#    2. Loop-mount its partitions (boot + root, or a single root partition).
#    3. Bind /dev,/proc,/sys and (on non-ARM hosts) register qemu so we can
#       chroot into the ARM root filesystem.
#    4. Stage the app at /opt/incubator and run install.sh in IMAGE_BUILD mode
#       (packages, venv, pip wheels, env file, enable the services).
#    5. Enable SSH, unmount, and repack into dist/incubator-v3-<ver>-<date>.img.xz
#
#  Pi-specific steps (PARTUUID rewrite of cmdline.txt/fstab, config.txt
#  overlays + boot-safe GPIO levels, userconf.txt) run only when the boot
#  partition carries a cmdline.txt — i.e. the base really is a Raspberry Pi
#  image. Point --base at a single-partition image (e.g. Armbian for an Orange
#  Pi / Rock Pi / Banana Pi) and the same pipeline stages the app, runs
#  install.sh and verifies the generic checks.
#
#  REQUIREMENTS (build host): Linux, root (sudo), ~6 GB free disk. On x86 hosts
#  qemu-user-static is auto-installed on Debian/Ubuntu. Building on a Raspberry
#  Pi / arm64 host needs no emulation at all.
#
#  Usage:
#    sudo ./build_image.sh [options]
#
#  Options:
#    --base <path|url>   Base image (.img/.img.xz/.zip) or download URL.
#                        Default: a PINNED, known-good Raspberry Pi OS Lite
#                        (Bookworm, arm64) release — not "latest", which shifted
#                        to a base that no longer boots the Pi Zero 2 W.
#                        A single-partition (non-Pi) image is accepted too.
#    --out <dir>         Output directory (default: ./dist).
#    --hostname <name>   Device hostname (default: incubator).
#    --wifi-country <CC> Wi-Fi regulatory country, ISO 3166-1 alpha-2 (default:
#                        US). Required for the radio to transmit — many kernels
#                        keep the WLAN rfkill-blocked until a country is set.
#    --user <name>       Create an OS login user for SSH (default: incubator
#                        when --password is given).
#    --password <pw>     Password for the SSH login user (enables SSH login).
#    --no-ssh            Do not enable the SSH server.
#    --no-compress       Leave the raw .img instead of compressing to .img.xz.
#    --grow <MB>         Extra root-fs space added before installing (default 2048).
#    --cache <dir>       Where to cache downloaded base images (default: ./.image-cache).
#    -h, --help          Show this help.
#
#  NOTE: the OS login user (--user/--password) is only for SSH/console access.
#  The incubator operator ACCOUNT (the web login) is created in the browser
#  during first-boot onboarding — that is the "account creation + user auth".
# =============================================================================
set -Eeuo pipefail   # -E so the ERR trap is inherited by functions/subshells

# ── Defaults ─────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
# PINNED base image. We deliberately do NOT use `raspios_lite_arm64_latest`:
# Raspberry Pi moves "latest" forward, and a newer base (different boot/partition
# layout) stopped booting on the Pi Zero 2 W after our resize + PARTUUID steps
# — the symptom is a dark ACT LED (firmware never loads the kernel) even though
# older builds of this exact pipeline booted fine. Pinning to a known-good
# Bookworm arm64 Lite release makes the produced image boot reproducibly.
# Override with --base <url|path> if you want a different base.
BASE_IMAGE_URL_DEFAULT="https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz"
# Ordered fallbacks tried when the primary can't be fetched (the .org mirror and
# a couple of other known-good dated Bookworm releases), so a single moved file
# never breaks the build.
BASE_IMAGE_FALLBACKS=(
    "https://downloads.raspberrypi.org/raspios_lite_arm64/images/raspios_lite_arm64-2024-11-19/2024-11-19-raspios-bookworm-arm64-lite.img.xz"
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-10-22/2024-10-22-raspios-bookworm-arm64-lite.img.xz"
    "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2024-07-04/2024-07-04-raspios-bookworm-arm64-lite.img.xz"
    "https://downloads.raspberrypi.org/raspios_lite_arm64/images/raspios_lite_arm64-2024-07-04/2024-07-04-raspios-bookworm-arm64-lite.img.xz"
)
BASE_INPUT=""
OUT_DIR="${REPO_DIR}/dist"
CACHE_DIR="${REPO_DIR}/.image-cache"
PI_HOSTNAME="incubator"
WIFI_COUNTRY="US"   # ISO 3166-1 alpha-2; unblocks the WLAN radio on Bookworm
SSH_USER=""
SSH_PASS=""
ENABLE_SSH=1
COMPRESS=1
GROW_MB=2048   # extra root-fs headroom for the pre-baked venv + apt packages
               # (~1 GB is used; rest is slack). Kept small so the image flashes
               # onto an 8 GB card — the root fs auto-expands to fill the card on
               # first boot, so a compact image loses nothing.

# ── Colour helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
step()  { echo -e "${BLUE}[STEP]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
# In GitHub Actions, also emit a ::error:: annotation so the failure reason is
# visible on the run summary (not just buried in the step log body).
error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
    [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::error title=build_image.sh::$*"
    exit 1
}

# Print the banner comment (between the two ===== delimiter lines) as help text.
usage() { sed -n '3,/^# ===/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)        BASE_INPUT="${2:-}"; shift 2 ;;
        --out)         OUT_DIR="${2:-}"; shift 2 ;;
        --hostname)    PI_HOSTNAME="${2:-}"; shift 2 ;;
        --wifi-country) WIFI_COUNTRY="${2:-}"; shift 2 ;;
        --user)        SSH_USER="${2:-}"; shift 2 ;;
        --password)    SSH_PASS="${2:-}"; shift 2 ;;
        --cache)       CACHE_DIR="${2:-}"; shift 2 ;;
        --no-ssh)      ENABLE_SSH=0; shift ;;
        --no-compress) COMPRESS=0; shift ;;
        --grow)        GROW_MB="${2:-}"; shift 2 ;;
        -h|--help)     usage ;;
        *) error "Unknown option: $1 (try --help)" ;;
    esac
done

[[ "$EUID" -ne 0 ]] && error "Run as root: sudo $0 $*"
[[ "$(uname -s)" == "Linux" ]] || error "This builder must run on Linux (needs loop mounts + chroot)."

# Normalise + validate the Wi-Fi country (ISO 3166-1 alpha-2).
WIFI_COUNTRY="$(printf '%s' "${WIFI_COUNTRY}" | tr '[:lower:]' '[:upper:]')"
[[ "$WIFI_COUNTRY" =~ ^[A-Z]{2}$ ]] || error "Invalid --wifi-country '${WIFI_COUNTRY}' (expect a 2-letter code like US, GB, DE)."

VERSION="$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/app/version.py" 2>/dev/null || echo dev)"
HOST_ARCH="$(uname -m)"
WORK_DIR="${OUT_DIR}/.build"
ROOT_MNT="${WORK_DIR}/rootfs"
BOOT_MNT=""
LOOP_DEV=""
ROOT_PART=""        # e.g. /dev/loop0p2 (Pi) or /dev/loop0p1 (single-partition base)
BOOT_PART=""        # e.g. /dev/loop0p1, or "" when the base has no boot partition
ROOT_PART_NUM=2
IS_PI_IMAGE=0       # 1 when the boot partition carries cmdline.txt (Raspberry Pi)
RESOLV_BACKED_UP=0
NEED_EMULATION=0   # set to 1 in ensure_tools when the host is not arm64

# Unmount a path with a REAL (non-lazy) umount, retrying a few times. A lazy
# umount (-lf) detaches the mount but lets writeback happen later — if we then
# detach the loop device the partition edits never reach the backing .img. So
# only fall back to lazy if a real umount genuinely keeps failing, and sync
# afterwards to give that writeback the best chance of landing.
umount_real() {
    local mp="$1" i
    mountpoint -q "${mp}" 2>/dev/null || return 0
    for i in 1 2 3 4 5; do
        umount "${mp}" 2>/dev/null && return 0
        sync; sleep 1
    done
    warn "Real umount of ${mp} failed; falling back to lazy umount (edits may not flush)."
    umount -lf "${mp}" 2>/dev/null
    sync
}

# ── Cleanup (idempotent; runs on any exit) ───────────────────────────────────
cleanup() {
    set +e
    # Restore the guest resolv.conf and remove the emulator we injected.
    if [[ -n "${ROOT_MNT}" && -d "${ROOT_MNT}" ]]; then
        [[ "${RESOLV_BACKED_UP}" == "1" && -e "${ROOT_MNT}/etc/resolv.conf.incubator-bak" ]] && \
            mv -f "${ROOT_MNT}/etc/resolv.conf.incubator-bak" "${ROOT_MNT}/etc/resolv.conf" 2>/dev/null
        rm -f "${ROOT_MNT}/usr/bin/qemu-aarch64-static" "${ROOT_MNT}/usr/bin/qemu-arm-static" 2>/dev/null
        # Flush page-cache writes (cmdline.txt/fstab edits) to the backing image
        # BEFORE unmounting + detaching the loop device, then unmount for real.
        sync
        for m in dev/pts dev proc sys boot/firmware boot ""; do
            umount_real "${ROOT_MNT}/${m}"
        done
    fi
    # One more sync so any buffered block writes hit the .img before we detach.
    sync
    [[ -n "${LOOP_DEV}" ]] && losetup -d "${LOOP_DEV}" 2>/dev/null
    LOOP_DEV=""
}
trap cleanup EXIT INT TERM

# Surface the exact failing line as a CI annotation for any command that trips
# `set -e` (not just our explicit error() calls), since the step log body isn't
# always reachable from the run summary.
on_err() {
    local rc=$?
    [[ -n "${GITHUB_ACTIONS:-}" ]] && \
        echo "::error title=build_image.sh::failed at line ${BASH_LINENO[0]:-?} (exit ${rc}): ${BASH_COMMAND}"
    return 0
}
trap on_err ERR

# ── Dependency checks ────────────────────────────────────────────────────────
ensure_tools() {
    step "Checking build dependencies..."
    local missing=()
    for t in losetup mount umount rsync xz openssl resize2fs e2fsck growpart; do
        command -v "$t" >/dev/null 2>&1 || missing+=("$t")
    done
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        missing+=("curl-or-wget")
    fi
    if ((${#missing[@]})); then
        if command -v apt-get >/dev/null 2>&1; then
            info "Installing host packages (util-linux rsync xz-utils openssl curl e2fsprogs cloud-guest-utils parted)"
            apt-get update -qq && apt-get install -y --no-install-recommends \
                util-linux rsync xz-utils openssl curl ca-certificates \
                e2fsprogs cloud-guest-utils parted file >/dev/null
        else
            error "Missing tools: ${missing[*]}. Install them and re-run."
        fi
    fi

    # zerofree zeroes the ext4 free space before compression so xz can crush it
    # (a mostly-empty 4.8 GB image shrinks to a few hundred MB). Best-effort: if
    # it can't be installed, finalize() just skips the zeroing and compresses a
    # larger-but-still-valid image.
    if ! command -v zerofree >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
        apt-get install -y --no-install-recommends zerofree >/dev/null 2>&1 \
            || warn "Could not install zerofree — free-space zeroing will be skipped (image compresses less)."
    fi

    # Cross-arch emulation is only needed when the host is not ARM.
    if [[ "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" || "$HOST_ARCH" == armv* ]]; then
        info "Native ARM host — no emulation needed."
        return
    fi
    NEED_EMULATION=1
}

# Register the qemu binfmt handler for the architecture the mounted root fs
# actually uses (aarch64 for Pi OS 64-bit / most Armbian; arm for 32-bit
# images). Called after mount_image so we can inspect the guest's /bin/sh.
ensure_emulation() {
    [[ "$NEED_EMULATION" == "1" ]] || return 0
    local guest_arch="aarch64" qemu_name="qemu-aarch64" desc=""
    if command -v file >/dev/null 2>&1; then
        desc="$(file -bL "${ROOT_MNT}/bin/sh" 2>/dev/null || true)"
        if [[ "$desc" == *"ARM aarch64"* ]]; then guest_arch="aarch64"; qemu_name="qemu-aarch64"
        elif [[ "$desc" == *"ARM,"* || "$desc" == *"ARM EABI"* ]]; then guest_arch="arm"; qemu_name="qemu-arm"
        elif [[ "$desc" == *"x86-64"* ]]; then info "x86-64 guest on an x86 host — no emulation needed."; NEED_EMULATION=0; return 0
        fi
    fi
    info "Guest root fs is ${guest_arch}; need ${qemu_name} emulation (host: ${HOST_ARCH})."

    # If a matching binfmt handler is already registered (e.g. CI ran
    # docker/setup-qemu-action, or qemu-user-static's binfmt service is up),
    # nothing more to do — that registration survives into the chroot.
    if ls /proc/sys/fs/binfmt_misc/${qemu_name}* >/dev/null 2>&1; then
        info "${guest_arch} emulation already registered."
    else
        # Otherwise try to install + register it ourselves.
        if command -v apt-get >/dev/null 2>&1; then
            info "Installing qemu-user-static for ${guest_arch} emulation..."
            apt-get update -qq
            apt-get install -y --no-install-recommends qemu-user-static binfmt-support >/dev/null \
                || warn "qemu-user-static install reported an error; checking registration anyway."
        fi
        command -v update-binfmts >/dev/null 2>&1 && update-binfmts --enable "${qemu_name}" 2>/dev/null || true
        systemctl restart systemd-binfmt 2>/dev/null || true
        # Last resort: register the aarch64 handler directly via the kernel interface.
        if [[ "$guest_arch" == "aarch64" ]] && ! ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1; then
            local qemu_bin; qemu_bin="$(command -v qemu-aarch64-static || true)"
            if [[ -n "$qemu_bin" ]]; then
                mountpoint -q /proc/sys/fs/binfmt_misc 2>/dev/null \
                    || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc 2>/dev/null || true
                # ELF magic for aarch64, with the 'F' (fix-binary) flag so the
                # interpreter is loaded once and stays valid inside the chroot.
                printf ':qemu-aarch64:M::\\x7fELF\\x02\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x02\\x00\\xb7\\x00:\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\x00\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xfe\\xff\\xff\\xff:%s:F' \
                    "$qemu_bin" > /proc/sys/fs/binfmt_misc/register 2>/dev/null || true
            fi
        fi
        ls /proc/sys/fs/binfmt_misc/${qemu_name}* >/dev/null 2>&1 || error \
            "${guest_arch} emulation is not registered. In CI add a 'docker/setup-qemu-action' step before this; locally run: sudo apt-get install --reinstall qemu-user-static binfmt-support"
        info "${guest_arch} emulation ready (host arch: ${HOST_ARCH})."
    fi

    # Copy the static emulator into the guest as a fallback. With the binfmt
    # 'F' (fix-binary) flag the kernel already holds the interpreter open, so
    # this is best-effort — a missing host binary must not abort the build.
    local qemu_bin; qemu_bin="$(command -v "${qemu_name}-static" || true)"
    [[ -n "$qemu_bin" ]] && cp "$qemu_bin" "${ROOT_MNT}/usr/bin/" 2>/dev/null || true
}

# ── Acquire + decompress the base image ──────────────────────────────────────
acquire_base() {
    mkdir -p "${CACHE_DIR}" "${WORK_DIR}"
    local src="${BASE_INPUT:-$BASE_IMAGE_URL_DEFAULT}"
    local archive=""

    if [[ -f "$src" ]]; then
        archive="$src"
        info "Using local base image: ${archive}"
    else
        # Cache key tied to the pinned release so bumping the pin never reuses a
        # stale (e.g. previously-"latest") base image. A custom URL is cached by
        # its file name.
        if [[ "$src" == "$BASE_IMAGE_URL_DEFAULT" ]]; then
            archive="${CACHE_DIR}/raspios_lite_arm64_2024-11-19.img.xz"
        else
            archive="${CACHE_DIR}/$(basename "$src")"
        fi
        if [[ -f "$archive" ]]; then
            info "Using cached download: ${archive}"
        else
            step "Downloading base image..."
            # Try the requested URL, then the pinned fallbacks (other CDN / dates)
            # so a single moved file never breaks the build.
            local urls=("$src")
            [[ "$src" == "$BASE_IMAGE_URL_DEFAULT" ]] && \
                urls+=("${BASE_IMAGE_FALLBACKS[@]}")
            local got=0 u
            for u in "${urls[@]}"; do
                info "Fetching ${u}"
                rm -f "${archive}.part"
                if command -v curl >/dev/null 2>&1; then
                    curl -fL --retry 3 --connect-timeout 30 -o "${archive}.part" "$u" && { got=1; break; }
                else
                    wget -O "${archive}.part" "$u" && { got=1; break; }
                fi
                warn "Download failed from ${u}"
            done
            [[ "$got" == "1" ]] || error "Could not download the base image (tried: ${urls[*]}). Pass --base <path|url>."
            # A captive portal / error page would download as HTML, not an image — catch it.
            if command -v file >/dev/null 2>&1 && file -bL "${archive}.part" | grep -qiE 'HTML|text'; then
                error "Downloaded file is not an image (got: $(file -bL "${archive}.part")) — mirror likely returned an error page."
            fi
            mv "${archive}.part" "$archive"
        fi
    fi

    WORK_IMG="${WORK_DIR}/incubator-build.img"
    step "Preparing writable image copy..."
    case "$archive" in
        *.xz)  xz -dc "$archive" > "$WORK_IMG" ;;
        *.zip) local tmpd; tmpd="$(mktemp -d)"; unzip -o "$archive" -d "$tmpd" >/dev/null
               cp "$(find "$tmpd" -name '*.img' | head -1)" "$WORK_IMG"; rm -rf "$tmpd" ;;
        *.img) cp "$archive" "$WORK_IMG" ;;
        *)     error "Unsupported base image type: $archive (expect .img, .img.xz, or .zip)" ;;
    esac
    [[ -s "$WORK_IMG" ]] || error "Prepared image is empty: $WORK_IMG"
}

# ── Mount partitions + prepare chroot ────────────────────────────────────────
mount_image() {
    # Grow the image first: the stock Lite root partition has very little free
    # space, not enough for the pre-baked venv + apt packages.
    if [[ "${GROW_MB:-0}" -gt 0 ]]; then
        step "Adding ${GROW_MB} MB of root-fs headroom..."
        truncate -s "+${GROW_MB}M" "$WORK_IMG"
    fi

    step "Loop-mounting image partitions..."
    LOOP_DEV="$(losetup -fP --show "$WORK_IMG")" || error "losetup failed"
    [[ -e "${LOOP_DEV}p1" ]] || { sleep 1; partprobe "$LOOP_DEV" 2>/dev/null || true; }
    # Layout: Raspberry Pi (and Ubuntu-for-Pi) images have p1 = FAT boot, p2 =
    # root. Armbian and most other SBC images have a single root partition.
    if [[ -e "${LOOP_DEV}p2" ]]; then
        BOOT_PART="${LOOP_DEV}p1"; ROOT_PART="${LOOP_DEV}p2"; ROOT_PART_NUM=2
    elif [[ -e "${LOOP_DEV}p1" ]]; then
        BOOT_PART=""; ROOT_PART="${LOOP_DEV}p1"; ROOT_PART_NUM=1
        info "Single-partition base image (no separate boot partition)."
    else
        error "No partitions found on ${LOOP_DEV}"
    fi

    if [[ "${GROW_MB:-0}" -gt 0 ]]; then
        step "Growing root partition + filesystem..."
        growpart "$LOOP_DEV" "$ROOT_PART_NUM" 2>/dev/null || parted -s "$LOOP_DEV" resizepart "$ROOT_PART_NUM" 100% 2>/dev/null \
            || warn "Could not grow partition table — install may run out of space."
        partprobe "$LOOP_DEV" 2>/dev/null || partx -u "$LOOP_DEV" 2>/dev/null || true
        e2fsck -fy "$ROOT_PART" >/dev/null 2>&1 || true
        resize2fs "$ROOT_PART" >/dev/null 2>&1 || warn "resize2fs failed — install may run out of space."
    fi

    mkdir -p "$ROOT_MNT"
    mount "$ROOT_PART" "$ROOT_MNT" || error "Cannot mount root partition"

    if [[ -n "$BOOT_PART" ]]; then
        # Bookworm mounts the FAT partition at /boot/firmware; older at /boot.
        if [[ -d "${ROOT_MNT}/boot/firmware" ]]; then BOOT_MNT="${ROOT_MNT}/boot/firmware"; else BOOT_MNT="${ROOT_MNT}/boot"; fi
        mkdir -p "$BOOT_MNT"
        mount "$BOOT_PART" "$BOOT_MNT" || error "Cannot mount boot partition"
        if [[ -f "${BOOT_MNT}/cmdline.txt" ]]; then
            IS_PI_IMAGE=1
            info "Raspberry Pi image detected (cmdline.txt on the boot partition)."
        else
            info "Boot partition has no cmdline.txt — treating as a generic (non-Pi) image."
        fi
    fi

    step "Preparing chroot environment..."
    mount --bind /dev     "${ROOT_MNT}/dev"
    mount --bind /dev/pts "${ROOT_MNT}/dev/pts"
    mount -t proc  proc    "${ROOT_MNT}/proc"
    mount -t sysfs sysfs   "${ROOT_MNT}/sys"

    # DNS for apt/pip inside the chroot.
    cp -a "${ROOT_MNT}/etc/resolv.conf" "${ROOT_MNT}/etc/resolv.conf.incubator-bak" 2>/dev/null || true
    RESOLV_BACKED_UP=1
    rm -f "${ROOT_MNT}/etc/resolv.conf"
    cp /etc/resolv.conf "${ROOT_MNT}/etc/resolv.conf" 2>/dev/null || echo "nameserver 1.1.1.1" > "${ROOT_MNT}/etc/resolv.conf"

    ensure_emulation
}

# ── Stage app + run the installer in the chroot ──────────────────────────────
install_app() {
    step "Staging application at /opt/incubator..."
    mkdir -p "${ROOT_MNT}/opt/incubator"
    # Never bake a developer's local database (accounts, Wi-Fi, claimed state)
    # into the image — a flashed unit would skip its own setup hotspot.
    rsync -a \
        --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='captures' --exclude='dist' --exclude='.image-cache' \
        --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.db.corrupt-*' \
        --exclude='database/backups' --exclude='database/control-persist.json' \
        "${REPO_DIR}/" "${ROOT_MNT}/opt/incubator/"

    if [[ -n "$PI_HOSTNAME" ]]; then
        echo "$PI_HOSTNAME" > "${ROOT_MNT}/etc/hostname"
        sed -i "s/127.0.1.1.*/127.0.1.1\t${PI_HOSTNAME}/" "${ROOT_MNT}/etc/hosts" 2>/dev/null || \
            echo -e "127.0.1.1\t${PI_HOSTNAME}" >> "${ROOT_MNT}/etc/hosts"
    fi

    step "Installing dependencies + services inside image (this can take a while under emulation)..."
    # INCUBATOR_IS_PI tells install.sh what the image is FOR: inside the chroot
    # /proc/device-tree belongs to the build host, so it cannot detect the board.
    chroot "$ROOT_MNT" /usr/bin/env \
        INCUBATOR_IMAGE_BUILD=1 DEBIAN_FRONTEND=noninteractive \
        INCUBATOR_IS_PI="${IS_PI_IMAGE}" \
        INCUBATOR_WIFI_COUNTRY="${WIFI_COUNTRY}" \
        /bin/bash /opt/incubator/install.sh /opt/incubator \
        || error "In-image install failed (see output above)."
}

# ── Headless boot configuration ──────────────────────────────────────────────
configure_boot() {
    if [[ "$IS_PI_IMAGE" == "1" ]]; then
        # Extract PARTUUIDs from the loop-mounted partitions and update boot config.
        local root_partuuid boot_partuuid
        root_partuuid="$(blkid -s PARTUUID -o value "$ROOT_PART")"
        boot_partuuid="$(blkid -s PARTUUID -o value "$BOOT_PART")"
        [[ -n "$root_partuuid" && -n "$boot_partuuid" ]] || error "Could not read PARTUUIDs"
        info "Root PARTUUID: ${root_partuuid}, Boot PARTUUID: ${boot_partuuid}"

        # Update kernel cmdline to use the real root PARTUUID (must stay ONE line).
        sed -i "s|root=[^[:space:]]*|root=PARTUUID=${root_partuuid}|" "${BOOT_MNT}/cmdline.txt"

        # Update fstab: point / and /boot[/firmware] at the real PARTUUIDs.
        if [[ -f "${ROOT_MNT}/etc/fstab" ]]; then
            sed -i -E \
                -e "s#^[^[:space:]]+([[:space:]]+/boot(/firmware)?[[:space:]])#PARTUUID=${boot_partuuid}\1#" \
                -e "s#^[^[:space:]]+([[:space:]]+/[[:space:]])#PARTUUID=${root_partuuid}\1#" \
                "${ROOT_MNT}/etc/fstab"
        fi

        # Guard: ensure cmdline.txt has a valid PARTUUID root= and no placeholders remain.
        grep -q "root=PARTUUID=" "${BOOT_MNT}/cmdline.txt" || error "cmdline root= not a PARTUUID"
        ! grep -qE "ROOTDEV|BOOTDEV" "${BOOT_MNT}/cmdline.txt" || error "placeholder left in cmdline"
    else
        info "Non-Pi image: leaving the bootloader config and fstab as shipped by the base image."
    fi

    if [[ "$ENABLE_SSH" == "1" ]]; then
        info "Enabling SSH server."
        [[ "$IS_PI_IMAGE" == "1" ]] && touch "${BOOT_MNT}/ssh"
        chroot "$ROOT_MNT" systemctl enable ssh 2>/dev/null \
            || chroot "$ROOT_MNT" systemctl enable sshd 2>/dev/null || true
    fi
    if [[ -n "$SSH_PASS" ]]; then
        local u="${SSH_USER:-incubator}"
        info "Creating OS login user '${u}' for SSH/console access."
        local hash; hash="$(echo "$SSH_PASS" | openssl passwd -6 -stdin)"
        if [[ "$IS_PI_IMAGE" == "1" ]]; then
            # Raspberry Pi OS creates the user from this file on first boot.
            echo "${u}:${hash}" > "${BOOT_MNT}/userconf.txt"
        else
            chroot "$ROOT_MNT" useradd -m -s /bin/bash -G sudo "$u" 2>/dev/null \
                || chroot "$ROOT_MNT" useradd -m -s /bin/bash "$u" 2>/dev/null || true
            echo "${u}:${hash}" | chroot "$ROOT_MNT" chpasswd -e 2>/dev/null \
                || warn "Could not set the password for '${u}' in the generic image."
        fi
    elif [[ -n "$SSH_USER" ]]; then
        warn "--user given without --password; skipping OS user creation."
    fi
}

# ── Verify the FINAL ARTIFACT ────────────────────────────────────────────────
# Verify the real bytes on disk, not the page-cache view we already released.
# Re-attach the written .img read-only and assert EVERY property the appliance
# depends on — boot config, services wired, app staged, open setup AP,
# per-device identity NOT pre-burned. Zero-tolerance gate: any failure aborts
# the build, so a broken image can never publish. (Born from build #12, which
# passed in-build guards yet shipped root=ROOTDEV because the writes never
# flushed to the backing image.)
VERIFY_OK=1
vfail() { echo "::error title=build_image.sh::verify: $*"; echo -e "${RED}[VERIFY]${NC} $*" >&2; VERIFY_OK=0; }
vpass() { echo "  [ok] $*"; }

# Pi-only checks on the boot partition: boot-by-PARTUUID, the I2C/RTC overlay
# and the boot-safe GPIO levels that keep the heater relay OFF during boot.
verify_pi_boot() {
    local b="$1"
    grep -q "root=PARTUUID=" "$b/cmdline.txt"      && vpass "cmdline root=PARTUUID" || vfail "cmdline has no PARTUUID root="
    ! grep -qE "ROOTDEV|BOOTDEV" "$b/cmdline.txt"  && vpass "cmdline has no placeholders" || vfail "cmdline still has ROOTDEV/BOOTDEV placeholder"
    [[ "$(awk 'END{print NR}' "$b/cmdline.txt")" -le 1 ]] && vpass "cmdline is a single line" || vfail "cmdline.txt is not a single line"
    # I2C bus + DS3231 RTC overlay baked (optional hardware; overlay no-ops if
    # no chip is fitted, but the lines must be present so a fitted RTC binds).
    if [[ -f "$b/config.txt" ]] && grep -qE '^[[:space:]]*dtparam=i2c_arm=on' "$b/config.txt"; then
        vpass "I2C bus enabled in config.txt"
    else
        vfail "dtparam=i2c_arm=on missing from config.txt — RTC/INA219 bus would be off"
    fi
    if [[ -f "$b/config.txt" ]] && grep -qE '^[[:space:]]*dtoverlay=i2c-rtc,ds3231' "$b/config.txt"; then
        vpass "DS3231 RTC overlay baked"
    else
        vfail "dtoverlay=i2c-rtc,ds3231 missing from config.txt — a fitted RTC would not bind"
    fi
    # Relays must be OFF from POWER-ON: firmware drives the pins to safe
    # levels before the kernel, else the heater blasts during every boot.
    if [[ -f "$b/config.txt" ]] && grep -q 'gpio=12,13,17,27=op,dh' "$b/config.txt" \
       && grep -q 'gpio=22,23,24,25=op,dl' "$b/config.txt"; then
        vpass "boot-safe GPIO states baked (relays off from power-on)"
    else
        vfail "gpio=...op,dh/op,dl lines missing from config.txt — heater relay would energize during boot"
    fi
    if [[ "$ENABLE_SSH" == "1" ]]; then
        [[ -e "$b/ssh" || -e "$b/ssh.txt" ]] && vpass "ssh enable flag present" || vfail "ssh flag file missing from boot partition"
    fi
}

# Pi-only checks on the root partition: fstab must boot by PARTUUID.
verify_pi_root() {
    local r="$1"
    grep -q "PARTUUID=" "$r/etc/fstab"             && vpass "fstab uses PARTUUID" || vfail "fstab not using PARTUUID"
    ! grep -qiE "ROOTDEV|BOOTDEV" "$r/etc/fstab"   && vpass "fstab has no placeholders" || vfail "fstab still has placeholder"
    # Hardware watchdog: reboots a hard-hung kernel (Restart= can't). Always
    # configured on a Pi (every Pi has /dev/watchdog).
    if [[ -f "$r/etc/systemd/system.conf.d/10-incubator-watchdog.conf" ]] \
       && grep -q "RuntimeWatchdogSec" "$r/etc/systemd/system.conf.d/10-incubator-watchdog.conf"; then
        vpass "hardware watchdog armed"
    else
        vfail "hardware watchdog config missing — a hung system would never reboot"
    fi
}

# Board-agnostic checks on the root partition.
verify_generic() {
    local r="$1" u

    # Wi-Fi country baked (radio stays rfkill-blocked without it → no hotspot).
    if [[ -f "$r/etc/systemd/system/incubator-wifi-country.service" ]] \
       && grep -q "iw reg set [A-Z][A-Z]" "$r/etc/systemd/system/incubator-wifi-country.service"; then
        vpass "wifi-country unit baked"
    else
        vfail "Wi-Fi regulatory country unit missing/incomplete (hotspot would stay rfkill-blocked)"
    fi

    # App staged at the version this build claims.
    grep -q "VERSION = \"${VERSION}\"" "$r/opt/incubator/app/version.py" 2>/dev/null \
        && vpass "app staged at version ${VERSION}" || vfail "staged app version != ${VERSION} (app/version.py mismatch or app missing)"
    [[ -x "$r/opt/incubator/.venv/bin/python" ]]   && vpass "venv baked" || vfail "venv missing — service cannot start"
    # uvicorn may live in the venv or (system-site-packages) in the distro python.
    if [[ -n "$(find "$r/opt/incubator/.venv/lib" -maxdepth 3 -type d -name uvicorn 2>/dev/null | head -1)" ]] \
       || [[ -n "$(find "$r/usr/lib" "$r/usr/local/lib" -maxdepth 3 -type d -name uvicorn 2>/dev/null | head -1)" ]]; then
        vpass "uvicorn installed"
    else
        vfail "uvicorn not installed — service cannot start"
    fi
    [[ -d "$r/opt/incubator/.git" ]]               && vpass "git checkout present (OTA can update)" || vfail "/opt/incubator/.git missing — OTA cannot update"

    # Env file: exists and the setup AP is OPEN (no baked Wi-Fi password).
    if [[ -f "$r/etc/incubator.env" ]]; then
        grep -q "^INCUBATOR_AP_PASSWORD=$" "$r/etc/incubator.env" \
            && vpass "setup AP is open (no baked password)" || vfail "INCUBATOR_AP_PASSWORD is non-empty — setup network would be locked"
        # Closed-loop temperature control must be ON out of the box, else a
        # flashed unit would monitor + alarm but never actually hold temp.
        grep -q "^CONTROL_DAEMON_ENABLED=true" "$r/etc/incubator.env" \
            && vpass "auto thermostat enabled (control daemon on)" || vfail "CONTROL_DAEMON_ENABLED != true — unit would not regulate temperature automatically"
        grep -q "^GPIO_BACKEND=" "$r/etc/incubator.env" \
            && vpass "HAL defaults present in env" || vfail "GPIO_BACKEND missing from /etc/incubator.env"
    else
        vfail "/etc/incubator.env missing"
    fi

    # Installed units: present, fully templated, and enabled.
    for u in incubator.service incubator-firstboot.service incubator-control.service incubator-ota.service incubator-gpio-safe.service; do
        [[ -f "$r/etc/systemd/system/$u" ]] || { vfail "unit $u not installed"; continue; }
        grep -qE "__INSTALL_DIR__|__DATA_DIR__" "$r/etc/systemd/system/$u" \
            && vfail "unit $u still contains a template placeholder" || vpass "unit $u installed + templated"
    done
    for u in incubator.service incubator-firstboot.service incubator-control.service incubator-gpio-safe.service; do
        [[ -L "$r/etc/systemd/system/multi-user.target.wants/$u" || -e "$r/etc/systemd/system/multi-user.target.wants/$u" ]] \
            && vpass "$u enabled" || vfail "$u not enabled (no multi-user.target.wants symlink)"
    done
    # The control daemon entry point must exist, else the thermostat crash-loops.
    [[ -f "$r/opt/incubator/app/control/__main__.py" ]] \
        && vpass "control daemon entry point present (python -m app.control)" \
        || vfail "app/control/__main__.py missing — incubator-control.service would crash-loop and never regulate temperature"
    # No developer database baked into the image.
    if find "$r/opt/incubator/database" -maxdepth 1 -name '*.db*' 2>/dev/null | grep -q .; then
        vfail "a SQLite database is baked into /opt/incubator/database — every flashed unit would inherit its accounts/Wi-Fi"
    else
        vpass "no database baked into the image"
    fi
    [[ -L "$r/etc/systemd/system/timers.target.wants/incubator-ota.timer" || \
       -e "$r/etc/systemd/system/timers.target.wants/incubator-ota.timer" ]] \
        && vpass "OTA timer enabled" || vfail "incubator-ota.timer not enabled — image would never auto-update"
    grep -q "SupplementaryGroups" "$r"/etc/systemd/system/incubator*.service 2>/dev/null \
        && vfail "units still declare SupplementaryGroups (gpio group dependency)" || vpass "units have no gpio-group dependency"

    # Per-device identity must be generated ON DEVICE, never pre-burned into
    # the image — else every flashed unit shares one ID/hostname/SSID.
    [[ ! -e "$r/etc/incubator-firstboot.done" ]] \
        && vpass "firstboot not pre-burned" || vfail "firstboot done-marker baked into image (identity would never provision)"
    [[ ! -e "$r/etc/incubator-device-id" ]] \
        && vpass "device id not pre-burned" || vfail "device id baked into image (every unit would share one identity)"

    # Networking stack the appliance depends on.
    [[ -x "$r/usr/sbin/NetworkManager" || -x "$r/usr/bin/NetworkManager" ]] && vpass "NetworkManager present" || vfail "NetworkManager missing — no hotspot/Wi-Fi"
    [[ -x "$r/usr/sbin/dnsmasq" || -x "$r/usr/bin/dnsmasq" ]] && vpass "dnsmasq present (hotspot DHCP/DNS)" || vfail "dnsmasq missing — hotspot clients would get no IP address"
    [[ -e "$r/usr/sbin/avahi-daemon" || -e "$r/usr/bin/avahi-daemon" ]] && vpass "avahi present (.local works)" || vfail "avahi-daemon missing — <hostname>.local would not resolve"
    [[ -L "$r/etc/systemd/system/multi-user.target.wants/avahi-daemon.service" || \
       -L "$r/etc/systemd/system/sockets.target.wants/avahi-daemon.socket" || \
       -L "$r/etc/systemd/system/dbus-org.freedesktop.Avahi.service" ]] \
        && vpass "avahi enabled" || vfail "avahi-daemon not enabled"
    [[ -L "$r/etc/systemd/system/multi-user.target.wants/NetworkManager.service" || \
       -L "$r/etc/systemd/system/dbus-org.freedesktop.NetworkManager.service" ]] \
        && vpass "NetworkManager enabled" || vfail "NetworkManager not enabled"
    [[ -f "$r/etc/NetworkManager/conf.d/10-incubator.conf" ]] \
        && vpass "NetworkManager drop-in present" || vfail "NetworkManager drop-in missing — Wi-Fi may stay unmanaged"
    [[ -f "$r/etc/NetworkManager/dnsmasq-shared.d/090-incubator-captive.conf" ]] \
        && vpass "captive DNS hijack conf present" || vfail "captive-portal DNS conf missing — setup page would not auto-open"
    [[ -f "$r/etc/avahi/services/incubator.service" ]] \
        && vpass "mDNS service advertisement present" || vfail "/etc/avahi/services/incubator.service missing"
    [[ -f "$r/etc/tmpfiles.d/incubator.conf" ]] \
        && vpass "tmpfiles runtime dir conf present" || vfail "/etc/tmpfiles.d/incubator.conf missing — /run/incubator would not exist at boot"

    # App-level watchdog: a HUNG (not crashed) app/daemon must be restarted.
    for u in incubator.service incubator-control.service; do
        if grep -q "^Type=notify" "$r/etc/systemd/system/$u" 2>/dev/null \
           && grep -q "^WatchdogSec=" "$r/etc/systemd/system/$u" 2>/dev/null; then
            vpass "$u app watchdog armed (Type=notify + WatchdogSec)"
        else
            vfail "$u missing Type=notify/WatchdogSec — a hung service would never restart"
        fi
    done
}

verify_artifact() {
    local img="$1" loop b r boot_part root_part is_pi=0
    step "Verifying the final image (zero-tolerance gate)..."
    VERIFY_OK=1

    loop="$(losetup -fP --show "$img")" || error "verify: losetup failed on ${img}"
    [[ -e "${loop}p1" ]] || { sleep 1; partprobe "$loop" 2>/dev/null || true; }
    if [[ -e "${loop}p2" ]]; then boot_part="${loop}p1"; root_part="${loop}p2"; else boot_part=""; root_part="${loop}p1"; fi
    b="$(mktemp -d)"; r="$(mktemp -d)"

    # ── Boot partition (Pi images) ──────────────────────────────────────
    if [[ -n "$boot_part" ]]; then
        if mount -o ro "$boot_part" "$b" 2>/dev/null; then
            if [[ -f "$b/cmdline.txt" ]]; then
                is_pi=1
                verify_pi_boot "$b"
            else
                vpass "generic image (no cmdline.txt) — Pi boot checks skipped"
            fi
            umount "$b" 2>/dev/null || umount -lf "$b" 2>/dev/null
        else
            vfail "cannot mount boot partition ${boot_part}"
        fi
    else
        vpass "single-partition image — no boot partition checks"
    fi

    # ── Root partition ──────────────────────────────────────────────────
    if mount -o ro "$root_part" "$r" 2>/dev/null; then
        [[ "$is_pi" == "1" ]] && verify_pi_root "$r"
        verify_generic "$r"
        umount "$r" 2>/dev/null || umount -lf "$r" 2>/dev/null
    else
        vfail "cannot mount root partition ${root_part}"
    fi

    rmdir "$b" "$r" 2>/dev/null || true
    losetup -d "$loop" 2>/dev/null || true
    [[ "$VERIFY_OK" == "1" ]] || error "Final artifact FAILED verification (see [VERIFY] lines / ::error annotations above)."
    info "Verified: final image passed all ${VERSION} appliance checks ($( [[ "$is_pi" == "1" ]] && echo 'Pi + generic' || echo 'generic'))."
}

# Zero the ext4 free space so xz can compress it away. zerofree operates on the
# block device of an UNMOUNTED filesystem, so we attach a fresh loop on the
# finished image (nothing mounted) and run it on the root partition. The boot
# (FAT) partition is left as-is. Best-effort: a missing/failed zerofree just
# yields a larger compressed image, never a broken one.
shrink_freespace() {
    local img="$1" loop part
    command -v zerofree >/dev/null 2>&1 || { warn "zerofree not installed — skipping free-space zeroing (image compresses less)."; return 0; }
    step "Zeroing ext4 free space for better compression..."
    loop="$(losetup -fP --show "$img")" || { warn "shrink: losetup failed — skipping zeroing."; return 0; }
    [[ -e "${loop}p1" ]] || { sleep 1; partprobe "$loop" 2>/dev/null || true; }
    part="${loop}p${ROOT_PART_NUM}"
    if [[ -e "$part" ]]; then
        e2fsck -fy "$part" >/dev/null 2>&1 || true   # zerofree refuses an unclean fs
        zerofree "$part" || warn "zerofree failed — image compresses less (continuing)."
    else
        warn "shrink: root partition not found on ${loop} — skipping zeroing."
    fi
    losetup -d "$loop" 2>/dev/null || true
}

# ── Repack the finished image ────────────────────────────────────────────────
finalize() {
    step "Unmounting and finalizing..."
    cleanup   # unmount + detach loop; safe to call again from the trap
    trap - EXIT INT TERM

    mkdir -p "$OUT_DIR"
    local stamp; stamp="$(date +%Y%m%d)"
    local out_img="${OUT_DIR}/incubator-v3-${VERSION}-${stamp}.img"
    mv "$WORK_IMG" "$out_img"
    rm -rf "$WORK_DIR"

    # Assert on the real packaged bytes before the slow xz step.
    verify_artifact "$out_img"

    # Crush the free space, then compress so the release download stays small.
    shrink_freespace "$out_img"

    local final="$out_img"
    if [[ "$COMPRESS" == "1" ]]; then
        step "Compressing (xz)... this is the slow part."
        rm -f "${out_img}.xz"
        xz -T0 -6 "$out_img"
        final="${out_img}.xz"
    fi

    local size sha
    size="$(du -h "$final" | cut -f1)"
    sha="$(sha256sum "$final" | cut -d' ' -f1)"
    echo ""
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Incubator v3 image ready!${NC}"
    echo -e "${GREEN}═══════════════════════════════════════════════════════════════${NC}"
    echo "  File:    ${final}"
    echo "  Size:    ${size}"
    echo "  SHA256:  ${sha}"
    echo ""
    echo "  Flash it to a microSD card (>= 4 GB), e.g.:"
    echo "    • Raspberry Pi Imager  → 'Use custom' → select this file"
    echo "    • balenaEtcher         → select this file → flash"
    [[ "$COMPRESS" == "1" ]] && echo "    • dd:  xz -dc '${final}' | sudo dd of=/dev/sdX bs=4M status=progress conv=fsync"
    echo ""
    echo "  Then insert the card and power on. On first boot the device broadcasts the"
    echo "  open Wi-Fi 'Incubator-XXXX'. Join it — the setup page opens by itself (or"
    echo "  open http://10.42.0.1:8000): your Wi-Fi, then a name + optional login."
    echo ""
}

# ── Run ──────────────────────────────────────────────────────────────────────
info "Building Incubator v3 SD-card image (version ${VERSION})"
ensure_tools
acquire_base
mount_image
install_app
configure_boot
finalize
