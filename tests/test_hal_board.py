"""detect_board() against fake root trees."""

import platform

from app.hal import BoardInfo, detect_board


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_raspberry_pi_tree(tmp_path):
    write(tmp_path, "proc/device-tree/model", "Raspberry Pi Zero 2 W Rev 1.0\x00")
    write(tmp_path, "etc/os-release", 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n')
    write(tmp_path, "boot/firmware/config.txt", "dtparam=i2c_arm=on\n")
    write(tmp_path, "proc/cpuinfo", "processor\t: 0\nmodel name\t: ARMv7\nSerial\t\t: 10000000abcdef12\nModel\t\t: Raspberry Pi Zero 2 W Rev 1.0\n")
    write(tmp_path, "etc/machine-id", "4c398f739c8447eb99d3fe100ad1b0fa\n")
    (tmp_path / "run/systemd/system").mkdir(parents=True)
    (tmp_path / "dev").mkdir()
    for n in ("gpiochip0", "gpiochip1", "i2c-1", "i2c-20", "null"):
        (tmp_path / "dev" / n).touch()

    info = detect_board(str(tmp_path))
    assert info.model == "Raspberry Pi Zero 2 W Rev 1.0"
    assert info.family == "raspberrypi" and info.is_raspberry_pi is True
    assert info.arch == platform.machine()
    assert (info.distro_id, info.distro_version) == ("debian", "12")
    assert info.boot_config == "/boot/firmware/config.txt"
    assert info.cpu_serial == "10000000abcdef12"
    assert info.machine_id == "4c398f739c8447eb99d3fe100ad1b0fa"
    assert info.has_systemd is True
    assert info.gpio_chips == ["gpiochip0", "gpiochip1"]
    assert info.i2c_buses == [1, 20]
    d = info.as_dict()
    assert set(d) == {
        "model", "family", "is_raspberry_pi", "arch", "distro_id", "distro_version", "boot_config",
        "cpu_serial", "machine_id", "has_systemd", "gpio_chips", "i2c_buses",
    }
    assert info.short_model.startswith("Raspberry Pi")


def test_legacy_boot_config_and_zero_serial(tmp_path):
    write(tmp_path, "sys/firmware/devicetree/base/model", "Raspberry Pi 3 Model B Plus Rev 1.3\x00")
    write(tmp_path, "boot/config.txt", "")
    write(tmp_path, "proc/cpuinfo", "Serial\t\t: 0000000000000000\n")
    info = detect_board(str(tmp_path))
    assert info.boot_config == "/boot/config.txt"
    assert info.cpu_serial is None  # all-zero serial is ignored
    assert info.is_raspberry_pi


def test_armbian_tree(tmp_path):
    write(tmp_path, "proc/device-tree/model", "Xunlong Orange Pi Zero 2\x00")
    write(tmp_path, "etc/armbian-release", "BOARD=orangepizero2\n")
    write(tmp_path, "boot/armbianEnv.txt", "overlays=i2c0\n")
    write(tmp_path, "etc/os-release", "ID=ubuntu\nVERSION_ID=22.04\n")
    info = detect_board(str(tmp_path))
    assert info.family == "armbian" and not info.is_raspberry_pi
    assert info.model == "Xunlong Orange Pi Zero 2"
    assert info.boot_config == "/boot/armbianEnv.txt"
    assert info.distro_id == "ubuntu"


def test_generic_and_missing_root(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    info = detect_board(str(empty))
    assert info == BoardInfo(arch=platform.machine())
    assert info.model == "Generic Linux" and info.family == "generic"
    assert info.boot_config is None and info.cpu_serial is None and info.machine_id is None
    assert info.gpio_chips == [] and info.i2c_buses == [] and info.has_systemd is False
    # nonexistent root must not raise either
    assert detect_board(str(tmp_path / "does-not-exist")).family == "generic"


def test_x86_uses_dmi_product_name(tmp_path):
    write(tmp_path, "sys/class/dmi/id/product_name", "NUC7i5BNH\n")
    write(tmp_path, "usr/lib/os-release", "ID=fedora\nVERSION_ID=40\n")
    info = detect_board(str(tmp_path))
    assert info.model == "NUC7i5BNH" and info.distro_id == "fedora"


def test_real_host_never_raises():
    info = detect_board()
    assert isinstance(info, BoardInfo)
    assert isinstance(info.as_dict(), dict)
