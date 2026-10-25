import random

from flexglove.control import GripController
from flexglove.processing import ChannelProcessor


def feed(ch, values, t0=0.0, dt=0.02):
    t = t0
    for v in values:
        ch.update(v, t)
        t += dt
    return t


def test_median_removes_spike_and_noise_is_small():
    ch = ChannelProcessor()
    rnd = random.Random(1)
    feed(ch, [213 + rnd.gauss(0, 3) for _ in range(200)] + [243] + [213] * 3)
    assert abs(ch.filtered - 213) < 3


def test_auto_calibration_detects_bend_either_polarity():
    for delta in (+600, -150):
        ch = ChannelProcessor()
        t = feed(ch, [213] * 40)
        assert ch.mode == "uncalibrated" and ch.bend == 0
        t = feed(ch, [213 + delta] * 40, t)
        assert ch.mode == "auto" and ch.bend > 0.95 and ch.bit == 1
        feed(ch, [213] * 40, t)
        assert ch.bend < 0.05 and ch.bit == 0


def test_noise_alone_never_triggers_auto_bend():
    ch = ChannelProcessor()
    rnd = random.Random(2)
    feed(ch, [213 + rnd.gauss(0, 3) for _ in range(2000)])
    assert ch.mode == "uncalibrated" and ch.bit == 0


def test_manual_calibration_and_hysteresis():
    ch = ChannelProcessor()
    ch.cal.open, ch.cal.fist = 200.0, 800.0
    t = feed(ch, [530] * 30)           # 0.55 — между порогами, остаётся 0
    assert ch.bit == 0
    t = feed(ch, [600] * 30, t)        # 0.67 > 0.6 — согнут
    assert ch.bit == 1
    feed(ch, [530] * 30, t)            # 0.55 — гистерезис держит 1
    assert ch.bit == 1


def test_controller_safety():
    c = GripController()
    c.set_link(True)
    c.step(1.0, 0.0); c.step(1.0, 0.1)
    assert c.command == 0.0                       # не разрешено — не двигается
    c.armed = True
    t = 0.1
    for _ in range(100):
        t += 0.02
        c.step(1.0, t)
    assert abs(c.command - c.config.max_close) < 1e-6   # ограничение сжатия
    c.hold = True
    c.step(0.0, t + 0.02)
    assert abs(c.command - c.config.max_close) < 1e-6   # фиксация
    c.hold = False
    msg = c.set_link(False)
    assert "Потеря связи" in msg and not c.armed
    c.step(0.0, t + 0.5)
    assert abs(c.command - c.config.max_close) < 1e-6   # при потере связи команда заморожена


def test_controller_rate_limit_and_deadband():
    c = GripController(armed=True, link_ok=True)
    c.step(0.0, 0.0)
    c.step(1.0, 0.1)
    assert abs(c.command - 0.15) < 1e-6          # 1.5/с * 0.1 с
    for i in range(100):
        c.step(0.5, 0.2 + i * 0.02)
    assert abs(c.command - 0.5) < 1e-6
    c.step(0.52, 2.3)                             # изменение меньше мёртвой зоны — не реагируем
    c.step(0.52, 2.4)
    assert abs(c.command - 0.5) < 1e-6
