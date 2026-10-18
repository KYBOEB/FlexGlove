"""Справочник действий, которые можно привязать к жестам.

Пока все действия — переключатели (toggle): жест включает/выключает объект на главном экране.
Новые типы (trigger, continuous) добавятся на следующих этапах.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    on_label: str
    off_label: str
    kind: str = "toggle"


ACTIONS: dict[str, Action] = {
    a.id: a
    for a in (
        Action("flashlight", "Фонарик", "Включён", "Выключен"),
        Action("eye", "Глаз", "Открыт", "Закрыт"),
        Action("fan", "Вентилятор", "Вращается", "Остановлен"),
    )
}


def catalog() -> list[dict]:
    return [asdict(a) for a in ACTIONS.values()]
