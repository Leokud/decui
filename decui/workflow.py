"""Declarative state transitions for persisted DECUI records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Transition:
    label: str
    source: str
    target: str
    roles: tuple[str, ...] = ()
    assigned_only: bool = False

    def allows(self, role: str | None, login: str | None = None, assignee: str | None = None) -> bool:
        return (not self.roles or role in self.roles) and (
            not self.assigned_only or (login is not None and login == assignee)
        )


@dataclass(frozen=True)
class WorkflowEvent:
    occurred_at: datetime
    source: str | None
    target: str
    action: str | None
    role: str | None = None
    login: str | None = None


@dataclass(frozen=True)
class Workflow:
    field: str
    initial: str
    states: tuple[str, ...]
    transitions: tuple[Transition, ...]
    post_states: tuple[str, ...] = ()
    assignee_field: str = "approval_assignee"

    def __post_init__(self) -> None:
        if self.initial not in self.states or len(set(self.states)) != len(self.states):
            raise ValueError("Проверьте начальный статус и уникальность статусов.")
        if not set(self.post_states) <= set(self.states):
            raise ValueError("Для проведения выберите статусы из маршрута.")
        actions: set[tuple[str, str]] = set()
        for step in self.transitions:
            if (
                not step.label.strip() or step.source not in self.states
                or step.target not in self.states or step.source == step.target
            ):
                raise ValueError(f"Недопустимый переход «{step.label}».")
            if any(not role.strip() for role in step.roles) or len(set(step.roles)) != len(step.roles):
                raise ValueError(f"Проверьте роли действия «{step.label}».")
            key = (step.source, step.label)
            if key in actions:
                raise ValueError(f"Действие «{step.label}» повторяется для статуса «{step.source}».")
            actions.add(key)

    def actions(self, state: str | None) -> tuple[Transition, ...]:
        return tuple(step for step in self.transitions if step.source == self.state(state))

    def state(self, value: str | None) -> str:
        return self.initial if value is None else value

    @property
    def uses_assignment(self) -> bool:
        return any(step.assigned_only for step in self.transitions)

    def available_actions(
        self, state: str | None, role: str, login: str | None = None, assignee: str | None = None,
    ) -> tuple[Transition, ...]:
        return tuple(step for step in self.actions(state) if step.allows(role, login, assignee))

    def validate_change(
        self, previous: str | None, current: str, *, creating: bool,
        role: str | None = None, action: str | None = None,
        login: str | None = None, assignee: str | None = None,
    ) -> None:
        if creating:
            if current != self.initial:
                raise ValueError(f"Новая запись должна иметь статус «{self.initial}».")
            return
        source = self.state(previous)
        if current == source and action is None and current in self.states:
            return
        candidates = [
            step for step in self.actions(previous)
            if step.target == current and (action is None or step.label == action)
        ]
        if not candidates:
            raise ValueError(f"Переход «{source}» → «{current}» не разрешён.")
        if not any(step.allows(role, login, assignee) for step in candidates):
            raise PermissionError("Для перехода нужны подходящая роль и, если задано, назначение согласующим.")
