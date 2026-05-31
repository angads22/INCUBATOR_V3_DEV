#!/usr/bin/env bash
# =============================================================================
#  build_image.sh — Build a ready-to-flash Incubator v3 SD-card image.
#
#  This single script turns the official Raspberry Pi OS Lite (64-bit) image
#  into a self-contained "appliance" image with the Incubator app, all of its
#  dependencies, and the systemd service PRE-INSTALLED. The result is a single
#  .img(.xz) you flash to a microSD card with Raspberry Pi Imager / balenaEtcher
#  / dd. On first boot the Pi needs NO internet: it starts the incubator
#  service, broadcasts a Wi-Fi setup hotspot, and walks you through Wi-Fi +
#  account creation / login in the browser.
#
#  How it works (no pi-gen required):
#    1. Download (or reuse) the stock Raspberry Pi OS Lite arm64 image.
#    2. Loop-mount its boot + root partitions.
#    3. Bind /dev,/proc,/sys and (on non-ARM hosts) register qemu-aarch64 so we
#       can chroot into the arm64 root filesystem.
#    4. Stage the app at /opt/incubator and run init_pi.sh in IMAGE_BUILD mode
#       (apt deps, venv, pip wheels, env file, enable the service).
#    5. Enable SSH, unmount, and repack into dist/incubator-v3-<ver>-<date>.img.xz
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
#                        Default: latest Raspberry Pi OS Lite arm64.
#    --out <dir>         Output directory (default: ./dist).
#    --hostname <name>   Pi hostname (default: incubator).
#    --user <name>       Create an OS login user for SSH (default: incubator
#                        when --password is given).
#    --password <pw>     Password for the SSH login user (enables SSH login).
#    --no-ssh            Do not enable the SSH server.
#    --no-compress       Leave the raw .img instead of compressing to .img.xz.
#    --cache <dir>       Where to cache downloaded base images (default: ./.image-cache).
#    -h, --help          Show this help.
#
#  NOTE: the OS login user (--user/--password) is only for SSH/console access.
#  The incubator operator ACCOUNT (the web login) is created in the browser
#  during first-boot onboarding — that is the "account creation + user auth".
# =============================================================================
set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_IMAGE_URL_DEFAULT="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
BASE_INPUT=""
OUT_DIR="${REPO_DIR}/dist"
CACHE_DIR="${REPO_DIR}/.image-cache"
PI_HOSTNAME="incubator"
SSH_USER=""
SSH_PASS=""
ENABLE_SSH=1
COMPRESS=1

# ── Colour helpers ───────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
step()  { echo -e "${BLUE}[STEP]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# Print the banner comment (between the two ===== delimiter lines) as help text.
usage() { sed -n '3,/^# ===/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0; }

# ── Parse args ───────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --base)        BASE_INPUT="${2:-}"; shift 2 ;;
        --out)         OUT_DIR="${2:-}"; shift 2 ;;
        --hostname)    PI_HOSTNAME="${2:-}"; shift 2 ;;
        --user)        SSH_USER="${2:-}"; shift 2 ;;
        --password)    SSH_PASS="${2:-}"; shift 2 ;;
        --cache)       CACHE_DIR="${2:-}"; shift 2 ;;
        --no-ssh)      ENABLE_SSH=0; shift ;;
        --no-compress) COMPRESS=0; shift ;;
        -h|--help)     usage ;;
        *) error "Unknown option: $1 (try --help)" ;;
    esac
done

[[ "$EUID" -ne 0 ]] && error "Run as root: sudo $0 $*"
[[ "$(uname -s)" == "Linux" ]] || error "This builder must run on Linux (needs loop mounts + chroot)."

VERSION="$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/app/version.py" 2>/dev/null || echo dev)"
HOST_ARCH="$(uname -m)"
WORK_DIR="${OUT_DIR}/.build"
ROOT_MNT="${WORK_DIR}/rootfs"
LOOP_DEV=""
RESOLV_BACKED_UP=0

# ── Cleanup (idempotent; runs on any exit) ───────────────────────────────────
cleanup() {
    set +e
    # Restore the guest resolv.conf and remove the emulator we injected.
    if [[ -n "${ROOT_MNT}" && -d "${ROOT_MNT}" ]]; then
        [[ "${RESOLV_BACKED_UP}" == "1" && -e "${ROOT_MNT}/etc/resolv.conf.incubator-bak" ]] && \
            mv -f "${ROOT_MNT}/etc/resolv.conf.incubator-bak" "${ROOT_MNT}/etc/resolv.conf" 2>/dev/null
        rm -f "${ROOT_MNT}/usr/bin/qemu-aarch64-static" 2>/dev/null
        for m in dev/pts dev proc sys boot/firmware boot ""; do
            mp="${ROOT_MNT}/${m}"
            mountpoint -q "${mp}" 2>/dev/null && { umount "${mp}" 2>/dev/null || umount -lf "${mp}" 2>/dev/null; }
        done
    fi
    [[ -n "${LOOP_DEV}" ]] && losetup -d "${LOOP_DEV}" 2>/dev/null
    LOOP_DEV=""
}
trap cleanup EXIT INT TERM

# ── Dependency checks ────────────────────────────────────────────────────────
ensure_tools() {
    step "Checking build dependencies..."
    local missing=()
    for t in losetup mount umount rsync xz openssl; do
        command -v "$t" >/dev/null 2>&1 || missing+=("$t")
    done
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        missing+=("curl-or-wget")
    fi
    if ((${#missing[@]})); then
        if command -v apt-get >/dev/null 2>&1; then
            info "Installing host packages: util-linux rsync xz-utils openssl curl"
            apt-get update -qq && apt-get install -y --no-install-recommends \
                util-linux rsync xz-utils openssl curl ca-certificates >/dev/null
        else
            error "Missing tools: ${missing[*]}. Install them and re-run."
        fi
    fi

    # Cross-arch emulation is only needed when the host is not arm64.
    if [[ "$HOST_ARCH" != "aarch64" && "$HOST_ARCH" != "arm64" ]]; then
        if ! command -v qemu-aarch64-static >/dev/null 2>&1; then
            if command -v apt-get >/dev/null 2>&1; then
                info "Installing qemu-user-static for arm64 emulation..."
                apt-get update -qq
                apt-get install -y --no-install-recommends qemu-user-static binfmt-support >/dev/null \
                    || error "Could not install qemu-user-static. Install it manually."
            else
                error "qemu-aarch64-static not found and apt-get unavailable. Install qemu-user-static."
            fi
        fi
        # Make sure the binfmt handler is actually registered.
        if ! ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1; then
            command -v update-binfmts >/dev/null 2>&1 && update-binfmts --enable qemu-aarch64 2>/dev/null || true
            systemctl restart systemd-binfmt 2>/dev/null || true
        fi
        ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1 \
            || error "arm64 binfmt handler not registered. Try: apt-get install --reinstall qemu-user-static binfmt-support"
        info "arm64 emulation ready (host arch: ${HOST_ARCH})."
    else
        info "Native arm64 host — no emulation needed."
    fi
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
        archive="${CACHE_DIR}/raspios_lite_arm64.img.xz"
        if [[ -f "$archive" ]]; then
            info "Using cached download: ${archive}"
        else
            step "Downloading Raspberry Pi OS Lite (arm64)..."
            if command -v curl >/dev/null 2>&1; then
                curl -fL --retry 3 -o "${archive}.part" "$src" || error "Download failed: $src"
            else
                wget -O "${archive}.part" "$src" || error "Download failed: $src"
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
    step "Loop-mounting image partitions..."
    LOOP_DEV="$(losetup -fP --show "$WORK_IMG")" || error "losetup failed"
    [[ -e "${LOOP_DEV}p2" ]] || { sleep 1; partprobe "$LOOP_DEV" 2>/dev/null || true; }
    [[ -e "${LOOP_DEV}p2" ]] || error "Root partition ${LOOP_DEV}p2 not found"

    mkdir -p "$ROOT_MNT"
    mount "${LOOP_DEV}p2" "$ROOT_MNT" || error "Cannot mount root partition"

    # Bookworm mounts the FAT partition at /boot/firmware; older at /boot.
    if [[ -d "${ROOT_MNT}/boot/firmware" ]]; then BOOT_MNT="${ROOT_MNT}/boot/firmware"; else BOOT_MNT="${ROOT_MNT}/boot"; fi
    mount "${LOOP_DEV}p1" "$BOOT_MNT" || error "Cannot mount boot partition"

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

    if [[ "$HOST_ARCH" != "aarch64" && "$HOST_ARCH" != "arm64" ]]; then
        cp "$(command -v qemu-aarch64-static)" "${ROOT_MNT}/usr/bin/"
    fi
}

# ── Stage app + run the installer in the chroot ──────────────────────────────
install_app() {
    step "Staging application at /opt/incubator..."
    mkdir -p "${ROOT_MNT}/opt/incubator"
    rsync -a \
        --exclude='.git' --exclude='.venv' --exclude='__pycache__' --exclude='*.pyc' \
        --exclude='captures' --exclude='dist' --exclude='.image-cache' \
        "${REPO_DIR}/" "${ROOT_MNT}/opt/incubator/"

    if [[ -n "$PI_HOSTNAME" ]]; then
        echo "$PI_HOSTNAME" > "${ROOT_MNT}/etc/hostname"
        sed -i "s/127.0.1.1.*/127.0.1.1\t${PI_HOSTNAME}/" "${ROOT_MNT}/etc/hosts" 2>/dev/null || \
            echo -e "127.0.1.1\t${PI_HOSTNAME}" >> "${ROOT_MNT}/etc/hosts"
    fi

    step "Installing dependencies + service inside image (this can take a while under emulation)..."
    chroot "$ROOT_MNT" /usr/bin/env \
        INCUBATOR_IMAGE_BUILD=1 DEBIAN_FRONTEND=noninteractive \
        /bin/bash /opt/incubator/init_pi.sh /opt/incubator \
        || error "In-image install failed (see output above)."
}

# ── Headless boot configuration ──────────────────────────────────────────────
configure_boot() {
    if [[ "$ENABLE_SSH" == "1" ]]; then
        info "Enabling SSH server."
        touch "${BOOT_MNT}/ssh"
        chroot "$ROOT_MNT" systemctl enable ssh 2>/dev/null || true
    fi
    if [[ -n "$SSH_PASS" ]]; then
        local u="${SSH_USER:-incubator}"
        info "Creating OS login user '${u}' for SSH/console access."
        local hash; hash="$(echo "$SSH_PASS" | openssl passwd -6 -stdin)"
        echo "${u}:${hash}" > "${BOOT_MNT}/userconf.txt"
    elif [[ -n "$SSH_USER" ]]; then
        warn "--user given without --password; skipping OS user creation."
    fi
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
    echo "  Then insert into the Pi and power on. On first boot the Pi broadcasts"
    echo "  Wi-Fi 'Incubator-XXXX'. Join it, open http://10.42.0.1:8000, and the"
    echo "  setup wizard handles Wi-Fi + account creation / login."
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
