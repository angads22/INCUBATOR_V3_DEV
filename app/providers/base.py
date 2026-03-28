from abc import ABC, abstractmethod

from ..domain import ControlResult, EnvironmentState


class HardwareProvider(ABC):
    @abstractmethod
    def read_environment(self) -> EnvironmentState:
        raise NotImplementedError

    @abstractmethod
    def set_heater(self, enabled: bool) -> ControlResult:
        raise NotImplementedError

    @abstractmethod
    def set_fan(self, enabled: bool) -> ControlResult:
        raise NotImplementedError

    @abstractmethod
    def run_turn_cycle(self) -> ControlResult:
        raise NotImplementedError

    @abstractmethod
    def reset_alarm(self) -> ControlResult:
        raise NotImplementedError
