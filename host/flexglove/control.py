"""Безопасный контроллер команд захвата (направление «носимый интерфейс телеуправления»).

Перчатка не управляет приводом напрямую: желаемое сжатие кисти проходит через контроллер,
который ограничивает команду и переводит систему в безопасное состояние при проблемах.

    сжатие кисти 0..1 ──▶ [разрешение] ──▶ [фиксация] ──▶ [мёртвая зона] ──▶ [ограничение
                          управления        захвата          ±3 %              скорости и
                                                                               диапазона] ──▶ команда захвату

Правила:
- пока управление не разрешено (ARM выключен) — команда не меняется;
- «фиксация» замораживает команду, чтобы оператор мог расслабить руку;
- потеря связи с перчаткой (нет данных > LINK_TIMEOUT_S) — команда замораживается,
  управление автоматически снимается; повторно включается только явным действием оператора;
- изменения меньше мёртвой зоны игнорируются (защита от дрожания и шума);
- скорость изменения команды ограничена, максимальное сжатие ограничено (аналог ограничения усилия).

Аварийная остановка реального оборудования должна быть независимой (аппаратная кнопка),
этот контроллер её не заменяет.
"""
from __future__ import annotations

from dataclasses import dataclass, field

LINK_TIMEOUT_S = 0.5


@dataclass
class ControllerConfig:
    deadband: float = 0.03        # доля диапазона
    max_rate: float = 1.5         # доля диапазона в секунду (полный ход ≈ 0,7 с)
    max_close: float = 0.9        # ограничение сжатия захвата


@dataclass
class GripController:
    config: ControllerConfig = field(default_factory=ControllerConfig)
    armed: bool = False
    hold: bool = False
    link_ok: bool = False
    target: float = 0.0           # желаемое сжатие (от руки)
    command: float = 0.0          # команда захвату после всех ограничений
    _accepted: float = 0.0        # цель после мёртвой зоны
    _last_t: float | None = None

    def set_link(self, ok: bool) -> str | None:
        """Возвращает текст события, если состояние связи изменилось."""
        if ok == self.link_ok:
            return None
        self.link_ok = ok
        if not ok and self.armed:
            self.armed = False
            return "Потеря связи: команда заморожена, управление снято"
        return "Связь с перчаткой есть" if ok else "Связи с перчаткой нет"

    def step(self, hand_grip: float, now: float) -> float:
        dt = 0.0 if self._last_t is None else max(0.0, min(0.2, now - self._last_t))
        self._last_t = now
        self.target = min(1.0, max(0.0, hand_grip))
        if not (self.armed and self.link_ok) or self.hold:
            return self.command
        goal = min(self.target, self.config.max_close)
        # мёртвая зона: маленькие колебания руки не меняют цель (кроме упора в границы)
        if abs(goal - self._accepted) >= self.config.deadband or goal in (0.0, self.config.max_close):
            self._accepted = goal
        diff = self._accepted - self.command
        max_step = self.config.max_rate * dt
        self.command += max(-max_step, min(max_step, diff))
        return self.command

    @property
    def state(self) -> str:
        if not self.link_ok:
            return "no_link"
        if not self.armed:
            return "disarmed"
        if self.hold:
            return "hold"
        return "active"
