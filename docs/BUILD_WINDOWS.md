# Building the SD-card image on Windows

`build_image.sh` needs Linux (loop mounts + chroot), so it can't run directly in
Windows `cmd`/PowerShell. You have three options — pick one.

---

## Option 1 — GitHub Actions (no Linux needed, easiest)

Let GitHub build the image for you and download the result.

1. Push this repo to GitHub (it already is, if you're reading this there).
2. Go to the **Actions** tab → **Build SD image** → **Run workflow**.
   - Optionally set a hostname (default `incubator`).
3. Wait for it to finish (~10–25 min — it builds on a **native ARM64** runner,
   so the app + dependencies install at full speed with no emulation).
4. Open the completed run → **Artifacts** → download **`incubator-sd-image`**
   (a `.zip`, ~1 GB). The run summary also prints the exact image size.
5. Flash it **without unzipping** (see **Flashing** below) — Raspberry Pi Imager
   and balenaEtcher both open the `.zip` and write the `.img` inside directly.
   (If you'd rather, unzip first to get the raw
   `incubator-v3-<version>-<date>.img` and flash that — same result.)

This uses the `.github/workflows/build-image.yml` workflow.

---

## Option 2 — WSL2 (build locally on Windows)

WSL2 runs a real Linux kernel, so the script works there.

1. Install WSL2 + Ubuntu (PowerShell as Administrator), then reboot if asked:
   ```powershell
   wsl --install -d Ubuntu
   ```
2. Open the **Ubuntu** terminal and build:
   ```bash
   sudo apt update && sudo apt install -y git
   git clone https://github.com/angads22/incubator_v3_dev
   cd incubator_v3_dev
   sudo ./build_image.sh
   ```
   The script auto-installs the rest (`qemu-user-static`, etc.).
3. The image is written to `dist/`. Copy it to Windows so Imager can see it:
   ```bash
   mkdir -p /mnt/c/Users/$USER/Downloads/incubator
   cp dist/*.img.xz /mnt/c/Users/$USER/Downloads/incubator/
   ```
   (Adjust the path to your Windows username.)

> Notes for WSL2: build on the Linux filesystem (e.g. your WSL home), **not**
> under `/mnt/c`, or loop mounts will fail. Use a recent WSL2
> (`wsl --update`). On the rare kernel without loop support, use Option 1 or 3.

---

## Option 3 — A Linux virtual machine

Install Ubuntu in VirtualBox / VMware / Hyper-V, give it ~8 GB disk headroom,
then follow the same steps as Option 2 inside the VM. Move the finished
`dist/*.img.xz` to the Windows host via a shared folder.

---

## Flashing the image (Windows)

Use **Raspberry Pi Imager** (recommended) or **balenaEtcher**. Both read the
downloaded `.zip` (or a raw `.img`) directly — you do **not** unzip it, and you
do **not** copy the file onto the card. The tool *writes* the image to the card.

> **"Image is too large" even on a big card?** That message is almost never
> about your card's capacity (the image is only ~4.7 GB; it auto-expands to fill
> the card on first boot). It usually means either (a) you tried to *copy* the
> file onto the card in File Explorer — FAT32 rejects files over 4 GB; use a
> flashing tool instead — or (b) an old Imager version. **Update to the latest
> Raspberry Pi Imager** and use **Write**.

**Raspberry Pi Imager**
1. Install/update from <https://www.raspberrypi.com/software/> (use the newest).
2. *Choose OS* → scroll down → **Use custom** → pick the downloaded `.zip`
   (or the `.img` if you unzipped it).
3. *Choose Storage* → your microSD card. Check it shows the card's real size
   (e.g. ~32 GB); if it shows tiny, try another reader or wipe the card first.
4. **Write**. (Skip the OS-customisation prompt — this image self-configures.)

**balenaEtcher**
1. Install from <https://etcher.balena.io/>.
2. *Flash from file* → the `.zip` (or `.img`) → select the SD card → **Flash**.

Then insert the card into the Pi and power on. It broadcasts the
**`Incubator-XXXX`** Wi-Fi hotspot; join it and open
**http://10.42.0.1:8000** to finish Wi-Fi setup and create your account.
