"""Действия, которые можно привязать к жестам-комбинациям.

Направление проекта — носимый интерфейс телеуправления: жесты дают дискретные команды
оператора, а степень сжатия кисти плавно управляет захватом через безопасный контроллер
(control.py).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    on_label: str
    off_label: str
    hint: str
    kind: str = "toggle"


ACTIONS: dict[str, Action] = {
    a.id: a
    for a in (
        Action("arm", "Управление захватом", "Разрешено", "Заблокировано",
               "Пока выключено, захват не реагирует на руку"),
        Action("hold", "Фиксация захвата", "Зафиксирован", "Следует за рукой",
               "Замораживает захват, чтобы можно было расслабить руку"),
        Action("light", "Подсветка рабочей зоны", "Включена", "Выключена",
               "Пример вспомогательной команды оператора"),
    )
}


def catalog() -> list[dict]:
    return [asdict(a) for a in ACTIONS.values()]
