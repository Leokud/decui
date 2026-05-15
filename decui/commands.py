from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

_COMMAND_MARKER = "__erp_command__"


@dataclass
class Command:
    name: str
    func: Callable
    description: str = ""

    def execute(self, instance: Any, *args: Any, **kwargs: Any) -> Any:
        return self.func(instance, *args, **kwargs)


def command(
    name: str,
    description: str = "",
) -> Callable[[Callable], Callable]:
    def decorator(func: Callable) -> Callable:
        setattr(func, _COMMAND_MARKER, Command(name, func, description))
        return func

    return decorator


def get_command_meta(func: Callable) -> Command | None:
    return getattr(func, _COMMAND_MARKER, None)
