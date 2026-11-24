from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

_COMMAND_MARKER = "__erp_command__"


@dataclass
class Command:
    """Action/command descriptor attached to an entity method."""

    name: str
    func: Callable
    description: str = ""

    def execute(self, instance: Any, *args: Any, **kwargs: Any) -> Any:
        return self.func(instance, *args, **kwargs)


# Universal alias for Command
Action = Command


def command(
    name: str,
    description: str = "",
) -> Callable[[Callable], Callable]:
    """Decorator to expose a method as a button/action on the entity form.

    Also available under the alias ``@action("Title")``.
    """
    def decorator(func: Callable) -> Callable:
        setattr(func, _COMMAND_MARKER, Command(name, func, description))
        return func

    return decorator


# Universal alias
action = command


def get_command_meta(func: Callable) -> Command | None:
    return getattr(func, _COMMAND_MARKER, None)


get_action_meta = get_command_meta

