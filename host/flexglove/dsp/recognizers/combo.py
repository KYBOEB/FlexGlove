"""Распознаватель комбинаций пальцев с удержанием и разделением «пальцы сжатия» / «пальцы команд» (решение D-21).
Перенесён из commands.py (там осталась обёртка для совместимости).

Проблема: с 2 сенсорами «оба согнуты» — это одновременно жест 11 и полное сжатие захвата, поэтому каждое
сжатие кулака переключало команду. Решение:

1. У каждого канала две роли (флажки в «Каналы и пальцы»): «сжатие» и «команды», можно обе.
   Степень сжатия захвата = среднее сгибов каналов с ролью «сжатие» (считает engine.py).
2. Управление НЕ активно (ARM снят): захват не движется, конфликта нет. Комбинация = биты каналов с ролью
   «команды» (остальные считаются разогнутыми), срабатывает после обычного удержания debounce_ms.
3. Управление активно (ARM включён):
   - каналы с ролью «сжатие» из комбинации исключаются: смотрим только «чистые» пальцы команд;
   - команда срабатывает после удержания ≥ ARMED_HOLD_S (1 с);
   - если с момента за GUARD_BEFORE_S до смены комбинации и до конца удержания сжатие изменилось больше чем
     на GUARD_GRIP_SPAN (15 %), комбинация считается частью сжатия и игнорируется до следующей смены:
     в настоящем кулаке все пальцы сгибаются одновременно, а осознанная команда — сгиб пальца команд при
     неподвижных пальцах сжатия;
   - привязка доступна, если её проекция на «чистые» пальцы команд не пустая; если проекции нескольких привязок
     совпали, выигрывает самая безопасная (ACTION_PRIORITY: снять ARM → фиксация → остальные).
4. При смене режима (ARM включили/сняли) текущая комбинация считается уже «использованной» — сначала её нужно
   изменить. Иначе жест 11, включивший ARM, через секунду сразу снял бы его.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .base import Event, Recognizer, Sample

ARMED_HOLD_S = 1.0          # удержание команды при активном управлении
GUARD_BEFORE_S = 0.3        # сколько секунд до смены комбинации учитывать движение сжатия
GUARD_GRIP_SPAN = 0.15      # изменение сжатия (max − min), при котором комбинация считается частью сжатия
ACTION_PRIORITY = ["arm", "hold"]   # при совпадении проекций: снять ARM важнее фиксации, фиксация — остальных
HISTORY_S = 3.0


@dataclass(frozen=True)
class Role:
    grip: bool = True
    cmd: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {"grip": self.grip, "cmd": self.cmd}


def default_roles(n: int) -> list[Role]:
    """По умолчанию: последний канал — только команды, остальные — сжатие + команды.
    Для 2 каналов: S1 ведёт захват (и участвует в командах при снятом ARM), S2 — палец команд."""
    if n <= 1:
        return [Role(True, True)] * n
    return [Role(True, True)] * (n - 1) + [Role(False, True)]


def normalize_roles(stored: list | None, n: int) -> list[Role]:
    """Роли из настроек (список {"grip":…, "cmd":…}) → ровно n ролей; недостающие — по умолчанию."""
    base = default_roles(n)
    out = []
    for i in range(n):
        r = stored[i] if stored and i < len(stored) and isinstance(stored[i], dict) else None
        out.append(Role(bool(r.get("grip")), bool(r.get("cmd"))) if r else base[i])
    return out


def priority(action_id: str) -> int:
    return ACTION_PRIORITY.index(action_id) if action_id in ACTION_PRIORITY else len(ACTION_PRIORITY)


def armed_bindings(bindings: dict[str, str], roles: list[Role]) -> dict[str, str]:
    """Какие привязки работают при активном управлении: проекция → действие (с учётом приоритета)."""
    pure = [i for i, r in enumerate(roles) if r.cmd and not r.grip]
    best: dict[str, str] = {}
    for combo, action in sorted(bindings.items()):
        if len(combo) != len(roles):
            continue
        proj = project(combo, pure)
        if "1" not in proj:
            continue
        if proj not in best or priority(action) < priority(best[proj]):
            best[proj] = action
    return best


def project(combo: str, keep: list[int]) -> str:
    """Оставляет биты каналов keep, остальные — 0 (длина строки не меняется)."""
    return "".join(c if i in keep else "0" for i, c in enumerate(combo))


class ComboRecognizer(Recognizer):
    """Состояние распознавания команд. Вызывается каждый такт ядра (50 Гц).

    update(sample) — общий интерфейс распознавателей: Sample.bits, Sample.grip и контекст
    {"armed": bool, "bindings": {комбинация: действие}, "debounce_s": float} → события
    Event("command", t, {"combo", "action"}) или Event("command_ignored", t, {"combo"}).
    decide(...) — то же самое с явными аргументами."""

    name = "combo"

    def __init__(self) -> None:
        self.roles: list[Role] = []
        self.candidate: str | None = None      # текущая комбинация (уже с учётом ролей и режима)
        self.since = 0.0
        self.committed: str | None = None      # комбинация, по которой уже принято решение
        self.blocked = False                   # кандидат отклонён: двигался вместе со сжатием
        self.armed_mode = False
        self.last_hold_s = 0.0
        self._grip: deque[tuple[float, float]] = deque()

    def reset(self) -> None:
        self.candidate = self.committed = None
        self.blocked = False
        self._grip.clear()

    # ------------------------------------------------------------------
    def combo_for(self, bits: tuple[int, ...], armed: bool) -> str:
        keep = [i for i, r in enumerate(self.roles[: len(bits)]) if r.cmd and not (armed and r.grip)]
        return project("".join(str(b) for b in bits), keep)

    def update(self, sample: Sample) -> list[Event]:
        ctx = sample.context
        result = self.decide(sample.bits, sample.grip, bool(ctx.get("armed")), sample.t,
                             ctx.get("bindings", {}), float(ctx.get("debounce_s", 0.2)))
        if result is None:
            return []
        combo, action = result
        if action == "":
            return [Event("command_ignored", sample.t, {"combo": combo})]
        return [Event("command", sample.t, {"combo": combo, "action": action})]

    def decide(self, bits: tuple[int, ...], grip: float, armed: bool, now: float,
               bindings: dict[str, str], debounce_s: float) -> tuple[str, str] | None:
        """Возвращает (комбинация, действие), когда команда должна сработать; иначе None.
        Если команда отклонена защитой от сопутствующего движения — возвращает (комбинация, "")."""
        self._grip.append((now, grip))
        while self._grip and now - self._grip[0][0] > HISTORY_S:
            self._grip.popleft()
        if not bits:
            return None

        combo = self.combo_for(bits, armed)
        self.last_hold_s = max(ARMED_HOLD_S, debounce_s) if armed else debounce_s
        if armed != self.armed_mode:            # режим сменился — текущая поза «использована»
            self.armed_mode = armed
            self.candidate = self.committed = combo
            self.since, self.blocked = now, False
            return None
        if combo != self.candidate:
            self.candidate, self.since, self.blocked = combo, now, False
        if combo == self.committed:
            return None

        if now - self.since < self.last_hold_s:
            return None
        self.committed = combo

        if armed:
            action = armed_bindings(bindings, self.roles).get(combo)
            if action is None:
                return None
            if self.grip_span(self.since - GUARD_BEFORE_S, now) > GUARD_GRIP_SPAN:
                self.blocked = True
                return (combo, "")
            return (combo, action)
        action = bindings.get(combo)
        return (combo, action) if action else None

    def grip_span(self, t_from: float, t_to: float) -> float:
        vals = [g for t, g in self._grip if t_from <= t <= t_to]
        return max(vals) - min(vals) if vals else 0.0

    def progress(self, now: float) -> float:
        """Доля удержания текущего кандидата (для индикатора в интерфейсе)."""
        if self.candidate is None or self.candidate == self.committed or "1" not in self.candidate:
            return 0.0
        hold = self.last_hold_s
        return min(1.0, (now - self.since) / hold) if hold > 0 else 1.0


CommandRecognizer = ComboRecognizer     # прежнее имя (commands.py, этап 2)
