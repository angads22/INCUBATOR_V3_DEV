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
BASE_IMAGE_URL_DEFAULT="https://downloads.raspberrypi.com/raspios_lite_arm64_latest"
BASE_INPUT=""
OUT_DIR="${REPO_DIR}/dist"
CACHE_DIR="${REPO_DIR}/.image-cache"
PI_HOSTNAME="incubator"
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

VERSION="$(grep -oP 'VERSION\s*=\s*"\K[^"]+' "${REPO_DIR}/app/version.py" 2>/dev/null || echo dev)"
HOST_ARCH="$(uname -m)"
WORK_DIR="${OUT_DIR}/.build"
ROOT_MNT="${WORK_DIR}/rootfs"
LOOP_DEV=""
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
        rm -f "${ROOT_MNT}/usr/bin/qemu-aarch64-static" 2>/dev/null
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
                e2fsprogs cloud-guest-utils parted >/dev/null
        else
            error "Missing tools: ${missing[*]}. Install them and re-run."
        fi
    fi

    # Cross-arch emulation is only needed when the host is not arm64.
    if [[ "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" ]]; then
        info "Native arm64 host — no emulation needed."
        return
    fi
    NEED_EMULATION=1

    # If an arm64 binfmt handler is already registered (e.g. CI ran
    # docker/setup-qemu-action, or qemu-user-static's binfmt service is up),
    # nothing more to do — that registration survives into the chroot.
    if ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1; then
        info "arm64 emulation already registered (host arch: ${HOST_ARCH})."
        return
    fi

    # Otherwise try to install + register it ourselves.
    if command -v apt-get >/dev/null 2>&1; then
        info "Installing qemu-user-static for arm64 emulation..."
        apt-get update -qq
        apt-get install -y --no-install-recommends qemu-user-static binfmt-support >/dev/null \
            || warn "qemu-user-static install reported an error; checking registration anyway."
    fi
    command -v update-binfmts >/dev/null 2>&1 && update-binfmts --enable qemu-aarch64 2>/dev/null || true
    systemctl restart systemd-binfmt 2>/dev/null || true
    # Last resort: register the handler directly via the kernel interface.
    if ! ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1; then
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
    ls /proc/sys/fs/binfmt_misc/qemu-aarch64* >/dev/null 2>&1 || error \
        "arm64 emulation is not registered. In CI add a 'docker/setup-qemu-action' step before this; locally run: sudo apt-get install --reinstall qemu-user-static binfmt-support"
    info "arm64 emulation ready (host arch: ${HOST_ARCH})."
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
            # Try the requested URL, then the other official CDN as a fallback.
            local urls=("$src")
            [[ "$src" == "$BASE_IMAGE_URL_DEFAULT" ]] && \
                urls+=("https://downloads.raspberrypi.org/raspios_lite_arm64_latest")
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
            # A captive portal / error page would download as HTML, not xz — catch it.
            if command -v file >/dev/null 2>&1 && ! file -bL "${archive}.part" | grep -qiE 'XZ|compress'; then
                error "Downloaded file is not an xz image (got: $(file -bL "${archive}.part")) — mirror likely returned an error page."
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
    [[ -e "${LOOP_DEV}p2" ]] || { sleep 1; partprobe "$LOOP_DEV" 2>/dev/null || true; }
    [[ -e "${LOOP_DEV}p2" ]] || error "Root partition ${LOOP_DEV}p2 not found"

    if [[ "${GROW_MB:-0}" -gt 0 ]]; then
        step "Growing root partition + filesystem..."
        growpart "$LOOP_DEV" 2 2>/dev/null || parted -s "$LOOP_DEV" resizepart 2 100% 2>/dev/null \
            || warn "Could not grow partition table — install may run out of space."
        partprobe "$LOOP_DEV" 2>/dev/null || partx -u "$LOOP_DEV" 2>/dev/null || true
        e2fsck -fy "${LOOP_DEV}p2" >/dev/null 2>&1 || true
        resize2fs "${LOOP_DEV}p2" >/dev/null 2>&1 || warn "resize2fs failed — install may run out of space."
    fi

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

    # Copy the static emulator into the guest as a fallback. With the binfmt
    # 'F' (fix-binary) flag the kernel already holds the interpreter open, so
    # this is best-effort — a missing host binary must not abort the build.
    if [[ "$NEED_EMULATION" == "1" ]]; then
        local qemu_bin; qemu_bin="$(command -v qemu-aarch64-static || true)"
        [[ -n "$qemu_bin" ]] && cp "$qemu_bin" "${ROOT_MNT}/usr/bin/" 2>/dev/null || true
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
    # Extract PARTUUIDs from the loop-mounted partitions and update boot config.
    local root_partuuid boot_partuuid
    root_partuuid="$(blkid -s PARTUUID -o value "${LOOP_DEV}p2")"
    boot_partuuid="$(blkid -s PARTUUID -o value "${LOOP_DEV}p1")"
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

# Verify the FINAL ARTIFACT (the real bytes on disk), not the page-cache view we
# already released. Re-attach the written .img read-only and assert the boot
# edits actually persisted — build #12 passed the in-build guards yet shipped
# root=ROOTDEV because the writes never flushed to the backing image.
verify_artifact() {
    local img="$1" loop b r ok=1
    step "Verifying boot config persisted in the final image..."
    loop="$(losetup -fP --show "$img")" || error "verify: losetup failed on ${img}"
    [[ -e "${loop}p2" ]] || { sleep 1; partprobe "$loop" 2>/dev/null || true; }
    b="$(mktemp -d)"; r="$(mktemp -d)"
    if mount -o ro "${loop}p1" "$b" 2>/dev/null; then
        grep -q "root=PARTUUID=" "$b/cmdline.txt"     || { echo "::error title=build_image.sh::artifact cmdline has no PARTUUID"; ok=0; }
        ! grep -qE "ROOTDEV|BOOTDEV" "$b/cmdline.txt"  || { echo "::error title=build_image.sh::artifact cmdline still has placeholder"; ok=0; }
        umount "$b" 2>/dev/null || umount -lf "$b" 2>/dev/null
    else
        echo "::error title=build_image.sh::verify: cannot mount ${loop}p1"; ok=0
    fi
    if mount -o ro "${loop}p2" "$r" 2>/dev/null; then
        grep -q "PARTUUID=" "$r/etc/fstab"            || { echo "::error title=build_image.sh::artifact fstab not using PARTUUID"; ok=0; }
        ! grep -qiE "ROOTDEV|BOOTDEV" "$r/etc/fstab"   || { echo "::error title=build_image.sh::artifact fstab still has placeholder"; ok=0; }
        umount "$r" 2>/dev/null || umount -lf "$r" 2>/dev/null
    else
        echo "::error title=build_image.sh::verify: cannot mount ${loop}p2"; ok=0
    fi
    rmdir "$b" "$r" 2>/dev/null || true
    losetup -d "$loop" 2>/dev/null || true
    [[ "$ok" == "1" ]] || error "Final artifact failed boot-config verification (see ::error annotations above)."
    info "Verified: final image boots by PARTUUID (no placeholders)."
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
