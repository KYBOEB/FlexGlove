"""Ориентация кисти в ядре (этап 4): IMU (протокол v0.3) → оси руки → смещение гироскопа → фильтр → поза.

Оси руки (решение D-24): X — к пальцам, Z — от тыльной стороны кисти, Y = Z × X (при ладони вниз — влево:
у правой руки к большому пальцу). Модуль MPU6050 может быть приклеен как угодно с шагом 90° —
«как закреплён модуль» выбирается пресетом (MOUNT_PRESETS): матрица M переводит вектор из осей датчика в оси руки,
a_рука = M · a_датчик (то же для угловой скорости).

Смещение нуля гироскопа:
- при подключении и по кнопке — 2 с замера, рука должна быть неподвижна (иначе замер повторяется);
- до первого замера — по первому участку покоя (детектор ImuRestDetector);
- дальше в покое смещение медленно уточняется (≈ 4 с памяти) — так уходит температурный дрейф.

Поза: фильтр (Madgwick по умолчанию) даёт q — поворот «рука → Земля» (ось Z Земли — вверх).

Для экрана (решение D-28) ориентация абсолютная по силе тяжести: наклоны (крен, тангаж) не обнуляются никогда —
рука опущена → модель пальцами вниз, ладонь вверх → модель ладонью вверх. Обнуляется только рысканье
(направление по горизонту, которое MPU6050 без магнитометра не знает): «Обнулить поворот» запоминает курс руки ψ0,
наружу отдаётся q_world = Rz(−ψ0) ⊗ q — после обнуления пальцы смотрят «вперёд» (ось X Земли = в экран).
Курс — направление пальцев на горизонт; если пальцы почти вертикальны, — по тыльной стороне кисти (см. heading_deg).

Для поворота захвата (D-25, в интерфейсе скрыт — D-26) остаётся прежний относительный поворот q_rel = q0* ⊗ q.
Крен для захвата — закрутка вокруг оси пальцев X (разложение «качание + закрутка»), устойчивая при любом наклоне
кисти, пока предплечье не вертикально (вокруг вертикали поворот видит только гироскоп — он дрейфует).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .dsp.filters import SampleClock
from .dsp.orientation import (ComplementaryFilter, MadgwickFilter, Quat, Vec, imu_from_v03, q_conj,
                              q_from_axis_angle, q_mul, q_normalize, q_rotate, q_to_euler)
from .dsp.segmentation import ImuRestDetector

CAPTURE_S = 2.0               # замер смещения при подключении и по кнопке
CAPTURE_MAX_STD_DPS = 1.0     # разброс гироскопа во время замера — иначе «рука двигалась»
TRACK_ALPHA = 0.005           # уточнение смещения в покое (на отсчёт, ≈ 4 с при 50 Гц)
YAW_DRIFT_DEG_PER_MIN = 0.3   # дрейф рысканья «в работе» по замеру на модели MPU6050 (docs/analysis/2026-11-01-imu.md)


def _mount(x: Vec, z: Vec) -> tuple[Vec, Vec, Vec]:
    """Матрица по строкам из образов осей датчика в осях руки: ось X датчика → x, ось Z датчика → z, Y = Z × X."""
    y = (z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0])
    cols = (x, y, z)
    return tuple(tuple(cols[c][r] for c in range(3)) for r in range(3))  # type: ignore[return-value]


_DIRS = {"fingers": ((1, 0, 0), "к пальцам"), "left": ((0, 1, 0), "влево (к большому у правой)"),
         "wrist": ((-1, 0, 0), "к запястью"), "right": ((0, -1, 0), "вправо (к мизинцу у правой)")}
_FACES = {"up": ((0, 0, 1), "лицом от тыльной стороны"), "down": ((0, 0, -1), "лицом к коже")}

MOUNT_PRESETS: dict[str, dict] = {
    f"x_{d}_z_{f}": {"title": f"X модуля {dt}, {ft}", "matrix": _mount(dv, fv)}
    for f, (fv, ft) in _FACES.items() for d, (dv, dt) in _DIRS.items()
}
DEFAULT_MOUNT = "x_fingers_z_up"


def apply_mount(m, v: Vec) -> Vec:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


HEADING_MIN_HORIZONTAL = 0.35   # пальцы ближе к вертикали (|горизонтальная часть| < 0,35, ≈ 70°) — курс по тыльной стороне


def heading_deg(q: Quat) -> float:
    """Курс руки (рысканье по горизонту), градусы −180…180: куда на горизонте смотрят пальцы.

    При почти вертикальных пальцах их проекция на горизонт мала и шумит — тогда курс берётся по тыльной стороне
    кисти: у руки «пальцами вверх» тыльная сторона смотрит назад (к оператору), «пальцами вниз» — вперёд.
    Так курс непрерывен при наклоне вперёд-назад (тангаже) и одинаков для поз «вперёд» и «вверх/вниз»."""
    fx, fy, fz = q_rotate(q, (1.0, 0.0, 0.0))
    if math.hypot(fx, fy) >= HEADING_MIN_HORIZONTAL:
        return math.degrees(math.atan2(fy, fx))
    bx, by, _ = q_rotate(q, (0.0, 0.0, 1.0))
    if fz > 0:
        bx, by = -bx, -by
    return math.degrees(math.atan2(by, bx))


def tilt_deg(q: Quat) -> tuple[float, float]:
    """Абсолютные наклоны по силе тяжести: (крен, тангаж) в градусах, тангаж > 0 — пальцы вверх."""
    roll, pitch, _ = q_to_euler(q)
    return roll, -pitch


def twist_deg(q: Quat, axis: int = 0) -> float:
    """Закрутка поворота q вокруг оси (0 — X): угол в градусах, −180…180 (разложение swing–twist)."""
    w, v = q[0], q[1 + axis]
    if abs(w) < 1e-12 and abs(v) < 1e-12:
        return 0.0
    a = math.degrees(2 * math.atan2(v, w))
    return (a + 180) % 360 - 180


@dataclass
class Capture:
    started: float
    samples: list

    def add(self, g: Vec) -> None:
        self.samples.append(g)


class ImuTracker:
    def __init__(self, mount: str = DEFAULT_MOUNT, method: str = "madgwick"):
        self.set_mount(mount)
        self.method = method
        self.reset()

    # ---------------- настройка ----------------
    def set_mount(self, mount: str) -> None:
        if mount not in MOUNT_PRESETS:
            raise ValueError(f"Неизвестное крепление «{mount}». Доступны: {', '.join(MOUNT_PRESETS)}")
        self.mount = mount
        self.matrix = MOUNT_PRESETS[mount]["matrix"]

    def reset(self) -> None:
        self.filter = MadgwickFilter() if self.method == "madgwick" else ComplementaryFilter()
        self.rest = ImuRestDetector()
        self.clock = SampleClock()
        self.bias: Vec = (0.0, 0.0, 0.0)
        self.bias_ok = False
        self.bias_source = ""
        self.capture: Capture | None = None
        self.message = ""
        self.q0: Quat = (1.0, 0.0, 0.0, 0.0)
        self.heading0 = 0.0                       # курс, принятый за «вперёд» (обнуление рысканья, D-28)
        self.zeroed = False
        self.zero_after_capture = False
        self.count = 0
        self.last_t = 0.0

    # ---------------- команды ----------------
    def start_capture(self, now: float, zero_after: bool = False) -> None:
        """Замер смещения гироскопа CAPTURE_S секунд (рука неподвижна)."""
        self.capture = Capture(now, [])
        self.zero_after_capture = zero_after
        self.message = "Калибровка гироскопа: держите руку неподвижно 2 с"

    def set_zero(self) -> None:
        """«Обнулить поворот»: текущий курс руки становится «вперёд» (экран). Наклоны не трогаются (D-28).
        Для поворота захвата (D-25) текущая поза запоминается целиком как исходная."""
        self.q0 = self.filter.q
        self.heading0 = heading_deg(self.filter.q)
        self.zeroed = True

    # ---------------- вход ----------------
    def update(self, raw_v03: tuple[float, ...], now: float) -> Quat:
        acc_s, gyro_s = imu_from_v03(raw_v03)
        acc, gyro = apply_mount(self.matrix, acc_s), apply_mount(self.matrix, gyro_s)
        dt = self.clock.tick(now)
        self.count += 1
        self.last_t = now
        at_rest = self.rest.update(gyro, acc, self.bias if self.bias_ok else None) == "rest"

        if self.capture is not None:
            self.capture.add(gyro)
            if now - self.capture.started >= CAPTURE_S:
                self._finish_capture()
        elif at_rest:
            if not self.bias_ok:                    # первый участок покоя — сразу берём среднее окна
                g = list(self.rest._g)
                self.bias = tuple(sum(v[k] for v in g) / len(g) for k in range(3))  # type: ignore[assignment]
                self.bias_ok, self.bias_source = True, "покой"
            else:
                self.bias = tuple(b + TRACK_ALPHA * (x - b) for b, x in zip(self.bias, gyro))  # type: ignore[assignment]

        corrected = (gyro[0] - self.bias[0], gyro[1] - self.bias[1], gyro[2] - self.bias[2])
        return self.filter.update(corrected, acc, dt)

    def _finish_capture(self) -> None:
        cap, self.capture = self.capture, None
        assert cap is not None
        n = len(cap.samples)
        if n < 10:
            self.message = "Калибровка не удалась: мало данных IMU"
            return
        means = [sum(g[k] for g in cap.samples) / n for k in range(3)]
        stds = [math.sqrt(sum((g[k] - means[k]) ** 2 for g in cap.samples) / n) for k in range(3)]
        if max(stds) > CAPTURE_MAX_STD_DPS:
            self.message = "Рука двигалась — калибровка гироскопа не принята, повторите (или подождите покоя)"
            return
        self.bias, self.bias_ok, self.bias_source = (means[0], means[1], means[2]), True, "замер 2 с"
        self.message = (f"Гироскоп откалиброван: смещение {means[0]:+.2f}; {means[1]:+.2f}; {means[2]:+.2f} °/с")
        if self.zero_after_capture:
            self.set_zero()

    # ---------------- выход ----------------
    @property
    def q(self) -> Quat:
        return self.filter.q

    @property
    def q_rel(self) -> Quat:
        """Поворот руки от исходной позы (в осях руки)."""
        return q_normalize(q_mul(q_conj(self.q0), self.filter.q))

    @property
    def q_world(self) -> Quat:
        """Ориентация руки для экрана: абсолютные наклоны, рысканье — от обнулённого курса (D-28)."""
        return q_normalize(q_mul(q_from_axis_angle((0.0, 0.0, 1.0), math.radians(-self.heading0)), self.filter.q))

    @property
    def wrist_roll_deg(self) -> float:
        """Крен кисти — закрутка вокруг оси пальцев относительно исходной позы."""
        return twist_deg(self.q_rel, 0)

    def snapshot(self, now: float) -> dict:
        qw = self.q_world
        roll, pitch = tilt_deg(qw)
        yaw = (heading_deg(self.filter.q) - self.heading0 + 180) % 360 - 180
        return {
            "q": [round(c, 5) for c in self.q_rel],          # относительно исходной позы (поворот захвата, D-25)
            "q_world": [round(c, 5) for c in qw],            # для экрана: «рука → мир», ось Z мира — вверх (D-28)
            "roll": round(roll, 1), "pitch": round(pitch, 1), "yaw": round(yaw, 1),
            "wrist_roll": round(self.wrist_roll_deg, 1),
            "bias": [round(b, 3) for b in self.bias], "bias_ok": self.bias_ok, "bias_source": self.bias_source,
            "calibrating": self.capture is not None, "at_rest": self.rest.at_rest, "zeroed": self.zeroed,
            "message": self.message, "mount": self.mount, "method": self.method,
            "yaw_drift_deg_min": YAW_DRIFT_DEG_PER_MIN,
            "stale": now - self.last_t > 1.0,
        }


def mount_catalog() -> list[dict]:
    return [{"id": k, "title": v["title"]} for k, v in MOUNT_PRESETS.items()]
