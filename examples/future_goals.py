"""Offline example for «Цель. Код будущего»."""

from __future__ import annotations

import datetime as dt
from typing import Any, ClassVar

from decui import (
    Boolean,
    Catalog,
    Choice,
    Column,
    Database,
    DateTime,
    Entity,
    ERPApplication,
    Integer,
    Ref,
    RefParam,
    Report,
    String,
    catalog,
    report,
)
from decui.fields import (
    BooleanField,
    ChoiceField,
    DateTimeField,
    IntegerField,
    RefField,
    StringField,
)

CREATED = "Создана"
ACTIVE = "Активна"
COMPLETED = "Завершена"
CANCELLED = "Отменена"
CELEBRATION = "Цель достигнута. Отличная работа!"


def tomorrow() -> dt.datetime:
    return (dt.datetime.now() + dt.timedelta(days=1)).replace(second=0, microsecond=0)


@catalog
class FutureGoal(Catalog):
    __verbose_name__ = "Цели"
    __section__ = "Планирование"
    __list_exclude__ = ("description",)
    __list_order__ = "deadline"
    __strike_completed__ = False

    name: StringField = String("Название", required=True)
    description: StringField = String("Описание", multiline=True)
    deadline: DateTimeField = DateTime("Срок выполнения", required=True, default=tomorrow)
    status: ChoiceField = Choice(
        "Статус",
        choices=[(value, value) for value in (CREATED, ACTIVE, COMPLETED, CANCELLED)],
        multi=False, required=True, default=CREATED,
    )
    progress: StringField = String("Прогресс", read_only=True, default="0%")

    # DECUI accepts omitted fields when creating a draft from the form.
    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    def before_save(self, db: Database) -> None:
        stages = db.filter(GoalStage, goal=self.id, order_by="position") if self.id is not None else []
        if stages and stages[-1].deadline > self.deadline:
            raise ValueError("Срок цели не может быть раньше срока её последнего этапа.")
        done = sum(stage.is_completed for stage in stages)
        count = len(stages)
        self.progress = f"{(200 * done + count) // (2 * count) if count else 0}%"
        if self.status == COMPLETED and not (count and done == count):
            raise ValueError("Завершить цель можно после выполнения всех этапов.")
        if self.status != CANCELLED and count and done == count:
            self.status = COMPLETED

    def prepare_copy(self, db: Database) -> None:
        self.status = CREATED
        self.progress = "0%"


@catalog
class GoalStage(Catalog):
    __verbose_name__ = "Задачи"
    __section__ = "Планирование"
    __list_exclude__ = ("description",)
    __list_order__ = "deadline"

    name: StringField = String("Название", required=True)
    goal: RefField = Ref(FutureGoal, "Цель", required=True, on_delete="CASCADE", related_list="Этапы цели")
    description: StringField = String("Описание", multiline=True)
    deadline: DateTimeField = DateTime("Срок выполнения", required=True, default=tomorrow)
    is_completed: BooleanField = Boolean("Выполнено", default=False)
    position: IntegerField = Integer("Номер этапа", default=0, visible=False)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)

    def prepare_copy(self, db: Database) -> None:
        self.is_completed = False
        self.position = 0
        stages = db.filter(GoalStage, goal=self.goal, order_by="-position", limit=1)
        parent = db.get(FutureGoal, self.goal)
        if parent is not None and stages:
            next_deadline = stages[0].deadline + dt.timedelta(minutes=1)
            if next_deadline <= parent.deadline:
                self.deadline = next_deadline

    def before_save(self, db: Database) -> None:
        parent = db.get(FutureGoal, self.goal)
        if parent is None:
            raise ValueError("Выберите существующую цель.")
        if self.deadline > parent.deadline:
            raise ValueError("Срок задачи не может быть позже срока цели.")

        previous = db.get(GoalStage, self.id) if self.id is not None else None
        others = [stage for stage in db.filter(GoalStage, goal=self.goal, order_by="position")
                  if stage.id != self.id]
        self.position = (
            previous.position if previous is not None and previous.goal == self.goal
            else (others[-1].position + 1 if others else 1)
        )
        earlier = [stage for stage in others if stage.position < self.position]
        later = [stage for stage in others if stage.position > self.position]
        if earlier and self.deadline <= earlier[-1].deadline:
            raise ValueError("Срок этапа должен быть позже срока предыдущего этапа.")
        if later and self.deadline >= later[0].deadline:
            raise ValueError("Срок этапа должен быть раньше срока следующего этапа.")
        if self.is_completed and any(not stage.is_completed for stage in earlier):
            raise ValueError("Сначала завершите предыдущие этапы.")
        if not self.is_completed and any(stage.is_completed for stage in later):
            raise ValueError("Сначала снимите отметку о выполнении с последующих этапов.")
        self._previous_goal: int | None = previous.goal if previous is not None else None
        self._save_message: str | None = None

    def after_save(self, db: Database) -> None:
        if self._previous_goal is not None and self._previous_goal != self.goal:
            self._update_goal(db, self._previous_goal)
        if self._update_goal(db, self.goal):
            self._save_message = CELEBRATION

    def after_delete(self, db: Database) -> None:
        self._update_goal(db, self.goal)

    @staticmethod
    def _update_goal(db: Database, goal_id: int) -> bool:
        parent = db.get(FutureGoal, goal_id)
        if parent is None:
            return False
        was_complete = parent.status == COMPLETED
        if was_complete:
            parent.status = ACTIVE
        db.save(parent)
        return not was_complete and parent.status == COMPLETED


@report
class GoalDynamics(Report):
    title = "Динамика достижения целей"
    auto_run = True
    sortable = False
    columns: ClassVar[list[Column]] = [
        Column("Цель / этап", "name", width=0),
        Column("Срок выполнения", "deadline", width=175),
        Column("Прогресс", "progress", width=110, align="right"),
    ]
    goal = RefParam("Цель", FutureGoal, representation_field="name")

    def execute(self, db: Database, **params: Any) -> list[dict[str, Any]]:
        selected = params.get("goal")
        filters = {"id": selected} if selected is not None else {}
        goals = db.filter(FutureGoal, status=ACTIVE, order_by="deadline", **filters)
        rows: list[dict[str, Any]] = []
        for goal in goals:
            rows.append({
                "name": goal.name, "progress": goal.progress, "_style": "group",
                "_entity_class": FutureGoal, "_entity_id": goal.id,
            })
            current_found = False
            for stage in db.filter(GoalStage, goal=goal.id, order_by="position"):
                style = "completed" if stage.is_completed else ("future" if current_found else "current")
                if not stage.is_completed:
                    current_found = True
                rows.append({
                    "name": "    " + stage.name,
                    "deadline": stage.deadline.strftime("%d.%m.%Y %H:%M"),
                    "_style": style, "_entity_class": GoalStage, "_entity_id": stage.id,
                })
        return rows


DEMO = (
    ("Подготовить портфолио дизайнера", "Собрать работы для отклика на стажировку.", ACTIVE, 3,
     ("Отобрать лучшие работы", "Описать процесс для каждой работы", "Собрать портфолио в PDF")),
    ("Подготовиться к экзамену по информатике", "Повторить программу и пройти пробный тест.", ACTIVE, 1,
     ("Составить список тем", "Повторить теорию", "Разобрать алгоритмы", "Решить 20 задач",
      "Пройти пробный тест", "Исправить ошибки")),
    ("Прочитать пять книг", "Вести заметки о прочитанном и обсудить любимую книгу.", ACTIVE, 3,
     ("Составить список книг", "Прочитать первую книгу", "Прочитать вторую книгу",
      "Прочитать третью книгу", "Прочитать четвёртую книгу", "Прочитать пятую книгу")),
    ("Подготовиться к забегу на 10 км", "Увеличивать дистанцию постепенно и завершить забег.", ACTIVE, 5,
     ("Пройти медицинский осмотр", "Подобрать обувь", "Составить план тренировок",
      "Пробежать 2 км без остановки", "Пробежать 5 км", "Пробежать 10 км")),
    ("Запустить школьный подкаст", "Подготовить пилотный выпуск с командой.", CANCELLED, 0,
     ("Выбрать тему первого выпуска", "Распределить роли в команде", "Записать пилотный выпуск")),
)


def populate_demo(db: Database, now: dt.datetime | None = None) -> None:
    """Create representative data for the first launch."""
    if db.list(FutureGoal):
        return
    now = (now or dt.datetime.now()).replace(second=0, microsecond=0)
    for name, description, status, completed, names in DEMO:
        goal = db.save(FutureGoal(
            name=name, description=description, status=status, deadline=now + dt.timedelta(days=32),
        ), validate=True)
        for index, stage_name in enumerate(names):
            date = now + dt.timedelta(days=(index - completed) * 4, minutes=45)
            db.save(GoalStage(
                name=stage_name, goal=goal.id, deadline=date, is_completed=index < completed,
            ), validate=True)


def seed_demo(db: Database, now: dt.datetime | None = None) -> None:
    """Install the demo in this application's database once."""
    db.initialize(lambda store: populate_demo(store, now))


def main() -> int:
    app = ERPApplication(
        title="Цель. Код будущего", app_id="FutureGoals",
        db_filename="goals.sqlite3", start=FutureGoal, first_run=populate_demo,
    )

    def is_active(entity: Entity) -> bool:
        if not isinstance(entity, GoalStage):
            return False
        goal = app.database.get(FutureGoal, entity.goal)
        return goal is not None and goal.status == ACTIVE

    app.enable_reminders([GoalStage], eligible=is_active)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
