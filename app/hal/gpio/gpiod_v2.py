"""libgpiod 2.x Python bindings (``pip install gpiod>=2.1``).

Call shapes used::

    import gpiod
    from gpiod.line import Direction, Value, Bias
    req = gpiod.request_lines(path, consumer=..., config={offset: gpiod.LineSettings(...)})
    req.set_value(offset, Value.ACTIVE); req.get_value(offset); req.release()
    gpiod.Chip(path).get_info().num_lines / .line_offset_from_id(name) / .get_line_info(offset).name
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
    return mod is not None and hasattr(mod, "request_lines")


def _line_mod(gpiod: Any) -> Any:
    line = getattr(gpiod, "line", None)
    if line is None:  # pragma: no cover - real gpiod always has it
        line = _util.optional_import("gpiod.line")
    if line is None:
        raise RuntimeError("gpiod.line (Direction/Value/Bias) missing — need gpiod>=2.0")
    return line


class _V2Output(OutputLine):
    def __init__(self, backend: "GpiodV2Backend", req: Any, offset: int, value_enum: Any) -> None:
        self._backend = backend
        self._req = req
        self._offset = offset
        self._Value = value_enum
        self._released = False

    def set(self, value: bool) -> None:
        self._req.set_value(self._offset, self._Value.ACTIVE if value else self._Value.INACTIVE)

    def get(self) -> bool:
        return self._req.get_value(self._offset) == self._Value.ACTIVE

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._req.release()
        finally:
            self._backend._forget(self)


class _V2Input(InputLine):
    def __init__(self, backend: "GpiodV2Backend", req: Any, offset: int, value_enum: Any) -> None:
        self._backend = backend
        self._req = req
        self._offset = offset
        self._Value = value_enum
        self._released = False

    def get(self) -> bool:
        return self._req.get_value(self._offset) == self._Value.ACTIVE

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._req.release()
        finally:
            self._backend._forget(self)


class GpiodV2Backend(GpiodCommon):
    name = "gpiod"
    api_version = 2

    def __init__(self, chip: str = "", dev_dir: str | None = None) -> None:
        super().__init__(chip=chip, dev_dir=dev_dir)
        gpiod = _util.optional_import("gpiod")
        if gpiod is None or not hasattr(gpiod, "request_lines"):
            raise RuntimeError("python gpiod 2.x not importable")
        self._gpiod = gpiod
        line = _line_mod(gpiod)
        self._Direction = line.Direction
        self._Value = line.Value
        self._Bias = line.Bias
        self._lines: list[Any] = []

    # -- chip introspection ----------------------------------------------------
    def _find_line_on_chip(self, chip_path: str, name: str) -> int | None:
        chip = self._gpiod.Chip(chip_path)
        try:
            try:
                return int(chip.line_offset_from_id(name))
            except (ValueError, KeyError, LookupError, OSError):
                return None
        finally:
            _close(chip)

    def chips(self) -> list[dict]:
        out: list[dict] = []
        for path in self.chip_paths():
            entry: dict[str, Any] = {"path": path}
            try:
                chip = self._gpiod.Chip(path)
                try:
                    info = chip.get_info()
                    entry["name"] = getattr(info, "name", None)
                    entry["label"] = getattr(info, "label", None)
                    entry["num_lines"] = int(getattr(info, "num_lines", 0))
                finally:
                    _close(chip)
            except Exception as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def line_name(self, chip: str, offset: int) -> str | None:
        try:
            c = self._gpiod.Chip(chip)
        except Exception:
            return None
        try:
            return getattr(c.get_line_info(offset), "name", None) or None
        except Exception:
            return None
        finally:
            _close(c)

    # -- requests -------------------------------------------------------------
    def request_output(self, pin: PinSpec, initial: bool, consumer: str = "incubator") -> OutputLine:
        path, offset = self.resolve(pin)
        settings = self._gpiod.LineSettings(
            direction=self._Direction.OUTPUT,
            output_value=self._Value.ACTIVE if initial else self._Value.INACTIVE,
        )
        req = self._gpiod.request_lines(path, consumer=consumer, config={offset: settings})
        line = _V2Output(self, req, offset, self._Value)
        with self._lock:
            self._lines.append(line)
        logger.debug("gpiod v2: output %s -> %s:%d (initial=%s)", pin, path, offset, initial)
        return line

    def request_input(self, pin: PinSpec, pull: Pull = "up", consumer: str = "incubator") -> InputLine:
        path, offset = self.resolve(pin)
        bias = {"up": self._Bias.PULL_UP, "down": self._Bias.PULL_DOWN}.get(pull, self._Bias.DISABLED)
        settings = self._gpiod.LineSettings(direction=self._Direction.INPUT, bias=bias)
        req = self._gpiod.request_lines(path, consumer=consumer, config={offset: settings})
        line = _V2Input(self, req, offset, self._Value)
        with self._lock:
            self._lines.append(line)
        logger.debug("gpiod v2: input %s -> %s:%d (pull=%s)", pin, path, offset, pull)
        return line

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


def _close(chip: Any) -> None:
    close = getattr(chip, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass
