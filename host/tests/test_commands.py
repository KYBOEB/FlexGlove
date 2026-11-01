"""D-21: команды-жесты не срабатывают от управления захватом.

Сценарии моделируют 2 сенсора при 50 Гц через полный конвейер ядра (фильтр → калибровка → сгиб → биты →
роли → распознавание команд → контроллер). Роли по умолчанию: S1 — сжатие + команды, S2 — только команды.
Привязки как у планировщика: 11 → ARM (управление), 01 → фиксация.
"""
from pathlib import Path

import pytest

from flexglove.commands import CommandRecognizer, Role, armed_bindings, default_roles, normalize_roles
from flexglove.engine import Engine
from flexglove.parser import Frame
from flexglove.storage import Storage

OPEN, FIST = 200.0, 800.0
DT = 0.02


class Glove:
    """Перчатка с 2 откалиброванными сенсорами; сгибы задаются функцией времени."""

    def __init__(self, bindings=None, roles=None):
        st = Storage(":memory:")
        st.put("calibration", {"open": [OPEN, OPEN], "fist": [FIST, FIST]})
        if roles is not None:
            st.put("channel_roles", roles)
        self.e = Engine(st, Path("."))
        self.e.source_info["connected"] = True
        for combo, action in (bindings or {"11": "arm", "01": "hold"}).items():
            self.e.set_binding(combo, action)
        self.t = 0.0
        self.run(1.0, 0, 0)

    def run(self, seconds, b1, b2):
        """b1, b2 — сгиб 0..1 или функция от времени с начала участка."""
        t0 = self.t
        while self.t < t0 + seconds - 1e-9:
            x = self.t - t0
            v1 = b1(x) if callable(b1) else b1
            v2 = b2(x) if callable(b2) else b2
            vals = (OPEN + (FIST - OPEN) * v1, OPEN + (FIST - OPEN) * v2)
            self.e._on_frame(Frame(kind="values", values=vals), self.t)
            self.e.tick(self.t)
            self.t += DT
        return self

    @property
    def armed(self):
        return self.e.controller.armed

    @property
    def hold(self):
        return self.e.controller.hold

    def gestures(self):
        return [ev for ev in self.e.events if ev["source"] == "gesture"]


def ramp(a, b, dur):
    return lambda x: a + (b - a) * min(1.0, x / dur)


def armed_glove():
    g = Glove()
    g.run(1.5, 1, 1)                 # жест 11 → ARM (ARM снят — обычное удержание 200 мс)
    assert g.armed
    g.run(1.0, 0, 0)                 # раскрыли ладонь
    assert g.armed
    return g


# ---------------- главный сценарий: «сжал кулак — команда не сработала» ----------------
def test_fist_while_armed_does_not_toggle_commands():
    g = armed_glove()
    n = len(g.gestures())
    g.run(0.3, ramp(0, 1, 0.3), ramp(0, 1, 0.3))     # сжал кулак (оба пальца вместе)
    g.run(3.0, 1, 1)                                 # держит кулак 3 с — дольше удержания команды
    assert g.armed and not g.hold
    assert len(g.gestures()) == n
    assert g.e.controller.command > 0.85             # захват при этом сжался (до ограничения 90 %)
    g.run(0.3, ramp(1, 0, 0.3), ramp(1, 0, 0.3))     # разжал
    g.run(1.5, 0, 0)
    assert g.armed and len(g.gestures()) == n


def test_repeated_fists_never_toggle():
    g = armed_glove()
    n = len(g.gestures())
    for _ in range(5):
        g.run(0.4, ramp(0, 1, 0.4), ramp(0, 1, 0.4)).run(1.5, 1, 1).run(0.4, ramp(1, 0, 0.4), ramp(1, 0, 0.4)).run(0.8, 0, 0)
    assert g.armed and len(g.gestures()) == n


def test_slow_fist_with_command_finger_lagging_is_ignored():
    """S2 сгибается на 0,2 с позже S1 — всё равно это сжатие, а не команда."""
    g = armed_glove()
    n = len(g.gestures())
    g.run(0.2, ramp(0, 0.6, 0.2), 0)
    g.run(0.4, ramp(0.6, 1, 0.4), ramp(0, 1, 0.4))
    g.run(2.0, 1, 1)
    assert g.armed and len(g.gestures()) == n
    assert any(ev["action"] == "ignored" for ev in g.e.events)


def test_ignored_combo_needs_change_before_it_can_fire():
    """Кулак отклонён; дальнейшее удержание той же позы не превращается в команду."""
    g = armed_glove()
    g.run(0.3, ramp(0, 1, 0.3), ramp(0, 1, 0.3)).run(5.0, 1, 1)
    assert g.armed


# ---------------- осознанная команда при активном управлении ----------------
def test_half_grip_then_hold_command_finger_disarms():
    g = armed_glove()
    g.run(1.0, 0.5, 0)                               # полусжатие — захват ≈ 50 %
    assert abs(g.e.controller.command - 0.5) < 0.06
    g.run(1.3, 0.5, 1)                               # держит палец команд ≥ 1 с, сжатие неподвижно
    assert not g.armed                               # проекции 11 и 01 совпали — выиграл «снять ARM»
    assert g.gestures()[0]["combo"] == "01"


def test_command_finger_held_less_than_one_second_does_nothing():
    g = armed_glove()
    g.run(1.0, 0.5, 0)
    g.run(0.8, 0.5, 1).run(1.0, 0.5, 0)
    assert g.armed and not g.hold


def test_grip_moving_during_command_hold_cancels_it():
    g = armed_glove()
    g.run(1.0, 0.2, 0)
    g.run(1.5, ramp(0.2, 0.8, 1.0), 1)               # палец команд согнут, но сжатие едет на 60 %
    assert g.armed


def test_arming_gesture_does_not_immediately_disarm():
    """11 включил ARM; рука ещё 3 с держит 11 — ARM не снимается (поза «использована»)."""
    g = Glove()
    g.run(1.0, 1, 1)
    assert g.armed
    g.run(3.0, 1, 1)
    assert g.armed


def test_disarmed_mode_keeps_short_hold_and_all_combos():
    g = Glove(bindings={"11": "arm", "01": "hold", "10": "light"})
    g.run(0.5, 1, 0)                                 # ARM снят — 10 работает за 200 мс
    assert g.e.light
    g.run(0.5, 0, 0).run(0.5, 0, 1)
    assert g.hold


def test_grip_uses_only_grip_channels():
    g = Glove()
    g.e.controller.armed = True
    g.run(1.5, 0, 1)                                 # согнут только палец команд (S2)
    assert g.e.grip < 0.05 and g.e.controller.command < 0.05


# ---------------- роли и привязки ----------------
def test_default_roles():
    assert default_roles(2) == [Role(True, True), Role(False, True)]
    assert default_roles(1) == [Role(True, True)]
    assert normalize_roles([{"grip": True, "cmd": False}], 2) == [Role(True, False), Role(False, True)]


def test_armed_bindings_priority_and_availability():
    roles = default_roles(2)
    assert armed_bindings({"11": "arm", "01": "hold", "10": "light"}, roles) == {"01": "arm"}
    assert armed_bindings({"01": "light"}, roles) == {"01": "light"}
    assert armed_bindings({"11": "arm"}, [Role(True, True), Role(True, True)]) == {}


def test_all_fingers_grip_means_no_gesture_commands_while_armed():
    g = Glove(roles=[{"grip": True, "cmd": True}, {"grip": True, "cmd": True}])
    g.run(1.0, 1, 1)
    assert g.armed
    g.run(1.0, 0, 0).run(1.0, 0.5, 0).run(2.0, 0.5, 1)
    assert g.armed


def test_roles_settings_persist(tmp_path):
    st = Storage(tmp_path / "r.db")
    e = Engine(st, tmp_path)
    e.set_settings(channel_roles=[{"grip": True, "cmd": False}, {"grip": False, "cmd": True}])
    e2 = Engine(Storage(tmp_path / "r.db"), tmp_path)
    e2._on_frame(Frame(kind="values", values=(200.0, 200.0)), 0.0)
    assert e2.roles() == [Role(True, False), Role(False, True)]


@pytest.mark.parametrize("armed", [False, True])
def test_recognizer_combo_projection(armed):
    r = CommandRecognizer()
    r.roles = default_roles(2)
    assert r.combo_for((1, 1), armed) == ("01" if armed else "11")
