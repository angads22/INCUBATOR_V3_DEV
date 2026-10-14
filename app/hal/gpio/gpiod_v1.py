"""libgpiod 1.x Python bindings (Debian/Ubuntu ``python3-libgpiod`` <= bookworm).

Call shapes used::

    chip = gpiod.Chip(path); line = chip.get_line(offset)
    line.request(consumer="x", type=gpiod.LINE_REQ_DIR_OUT, default_vals=[1])
    line.set_value(1); line.get_value(); line.release()
    chip.find_line(name); chip.num_lines()
    flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP for inputs
"""

from __future__ import annotations

import logging
from typing import Any

from .. import _util
from ._gpiod_common import GpiodCommon
from .base import InputLine, OutputLine, PinSpec, Pull

logger = logging.getLogger(__name__)


def is_available() -> bool:
    mod = _util.optional_import("gpiod")
    return mod is not None and hasattr(mod, "Chip") and hasattr(mod, "LINE_REQ_DIR_OUT")


class _V1Output(OutputLine):
    def __init__(self, backend: "GpiodV1Backend", line: Any) -> None:
        self._backend = backend
        self._line = line
        self._released = False

    def set(self, value: bool) -> None:
        self._line.set_value(1 if value else 0)

    def get(self) -> bool:
        return bool(self._line.get_value())

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._line.release()
        finally:
            self._backend._forget(self)


class _V1Input(InputLine):
    def __init__(self, backend: "GpiodV1Backend", line: Any) -> None:
        self._backend = backend
        self._line = line
        self._released = False

    def get(self) -> bool:
        return bool(self._line.get_value())

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._line.release()
        finally:
            self._backend._forget(self)


class GpiodV1Backend(GpiodCommon):
    name = "gpiod"
    api_version = 1

    def __init__(self, chip: str = "", dev_dir: str | None = None) -> None:
        super().__init__(chip=chip, dev_dir=dev_dir)
        gpiod = _util.optional_import("gpiod")
        if gpiod is None or not (hasattr(gpiod, "Chip") and hasattr(gpiod, "LINE_REQ_DIR_OUT")):
            raise RuntimeError("python gpiod 1.x not importable")
        self._gpiod = gpiod
        # v1 lines die with their Chip object, so keep chips open for the
        # lifetime of the backend.
        self._chips: dict[str, Any] = {}
        self._lines: list[Any] = []

    def _chip(self, path: str) -> Any:
        with self._lock:
            chip = self._chips.get(path)
            if chip is None:
                chip = self._gpiod.Chip(path)
                self._chips[path] = chip
            return chip

    # -- chip introspection ----------------------------------------------------
    def _find_line_on_chip(self, chip_path: str, name: str) -> int | None:
        chip = self._chip(chip_path)
        try:
            line = chip.find_line(name)
        except (ValueError, KeyError, LookupError, OSError):
            return None
        if line is None:
            return None
        offset = line.offset
        return int(offset() if callable(offset) else offset)

    def chips(self) -> list[dict]:
        out: list[dict] = []
        for path in self.chip_paths():
            entry: dict[str, Any] = {"path": path}
            try:
                chip = self._chip(path)
                entry["name"] = _call(chip, "name")
                entry["label"] = _call(chip, "label")
                num = _call(chip, "num_lines")
                entry["num_lines"] = int(num) if num is not None else None
            except Exception as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def line_name(self, chip: str, offset: int) -> str | None:
        try:
            line = self._chip(chip).get_line(offset)
            name = _call(line, "name")
            return name or None
        except Exception:
            return None

    # -- requests -------------------------------------------------------------
    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine:
        path, offset = self.resolve(pin)
        line = self._chip(path).get_line(offset)
        line.request(consumer=consumer, type=self._gpiod.LINE_REQ_DIR_OUT, default_vals=[1 if initial else 0])
        wrapped = _V1Output(self, line)
        with self._lock:
            self._lines.append(wrapped)
        logger.debug("gpiod v1: output %s -> %s:%d (initial=%s)", pin, path, offset, initial)
        return wrapped

    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine:
        path, offset = self.resolve(pin)
        line = self._chip(path).get_line(offset)
        flag_name = {"up": "LINE_REQ_FLAG_BIAS_PULL_UP", "down": "LINE_REQ_FLAG_BIAS_PULL_DOWN"}.get(pull, "LINE_REQ_FLAG_BIAS_DISABLE")
        # Bias flags arrived in libgpiod 1.5; older bindings simply lack them.
        flags = getattr(self._gpiod, flag_name, 0)
        line.request(consumer=consumer, type=self._gpiod.LINE_REQ_DIR_IN, flags=flags)
        wrapped = _V1Input(self, line)
        with self._lock:
            self._lines.append(wrapped)
        logger.debug("gpiod v1: input %s -> %s:%d (pull=%s)", pin, path, offset, pull)
        return wrapped

    def _forget(self, line: Any) -> None:
        with self._lock:
            if line in self._lines:
                self._lines.remove(line)

    def close(self) -> None:
        with self._lock:
            lines = list(self._lines)
        for line in lines:
            try:
                line.release()
            except Exception as exc:
                logger.debug("release failed: %s", exc)
        with self._lock:
            chips = list(self._chips.values())
            self._chips.clear()
        for chip in chips:
            close = getattr(chip, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass


def _call(obj: Any, attr: str) -> Any:
    value = getattr(obj, attr, None)
    return value() if callable(value) else value
