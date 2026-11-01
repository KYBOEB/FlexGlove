"""Ядро приложения.

Принимает сообщения от источника → разбирает (parser) → обрабатывает каждый канал (processing)
→ получает степень сгиба пальцев и комбинацию 0/1 → вызывает привязанные действия (жесты)
и управляет захватом через безопасный контроллер (control) → рассылает состояние в интерфейс.

Два режима входных данных определяются автоматически:
- values — значения АЦП (основной): фильтрация, калибровка, сгиб 0..1, состояние 0/1 с гистерезисом;
- bits   — готовые строки "01" (совместимость с этапом 0): сгиб = 0 или 1.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from pathlib import Path
from typing import Any

from .actions import ACTIONS
from .dsp.recognizers import CommandRecognizer, Role, Sample, armed_bindings, normalize_roles
from .control import LINK_TIMEOUT_S, GripController
from .parser import IMU_FIELDS, Frame, describe_raw, parse_message
from .dsp import presets
from .dsp.segmentation import HandSegmenter
from .imu import DEFAULT_MOUNT, MOUNT_PRESETS, ImuTracker
from .processing import CalibrationCapture, ChannelProcessor
from .recording import Recorder
from .storage import Storage

FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
DEFAULT_FINGER_MAP = ["thumb", "index", "middle", "ring", "pinky"]  # канал i -> палец
DEFAULT_DEBOUNCE_MS = 200
BROADCAST_HZ = 30
TICK_S = 0.02
MAX_CHANNELS = 8
MIN_RATE_HZ = 20.0          # ниже — предупреждение: управление станет рывками (прошивка шлёт 50 Гц)
WARN_GRACE_S = 2.5          # первые секунды после подключения не предупреждаем (частота ещё набирается)


class Engine:
    def __init__(self, storage: Storage, data_dir: Path | None = None):
        self.storage = storage
        self.finger_map: list[str] = storage.get("finger_map", DEFAULT_FINGER_MAP)
        if len(self.finger_map) < 5:  # настройки этапа 0 хранили 2 пальца
            self.finger_map = (self.finger_map + [f for f in DEFAULT_FINGER_MAP if f not in self.finger_map])[:5]
        self.debounce_ms: int = storage.get("debounce_ms", DEFAULT_DEBOUNCE_MS)
        self.recorder = Recorder((data_dir or Path("data")) / "recordings")

        self.mode = "none"                 # none | values | bits
        self.prefer_values = False
        self.channels: list[ChannelProcessor] = []
        self.tag_order: list[int] = []     # метки каналов в порядке номеров
        self.bits: tuple[int, ...] = ()
        self.combo_since = 0.0
        self.commands = CommandRecognizer()        # D-21: роли пальцев, удержание, защита от сжатия
        self._stored_roles: list | None = storage.get("channel_roles")
        self.grip = 0.0                            # степень сжатия кисти по пальцам сжатия
        self.segmenter = HandSegmenter()           # покой / движение руки (dsp/segmentation.py)
        mount = storage.get("imu_mount", DEFAULT_MOUNT)
        self.imu_tracker = ImuTracker(mount if mount in MOUNT_PRESETS else DEFAULT_MOUNT)   # ориентация (imu.py)
        self.hand: str = storage.get("hand", "right")
        self.pipeline_setting: dict[str, Any] = storage.get("pipeline", {"preset": presets.DEFAULT_PRESET})
        try:
            self.pipeline_config = presets.resolve(self.pipeline_setting.get("preset", ""), self.pipeline_setting.get("config"))
        except ValueError:                         # испорченная настройка — не ломаем запуск
            self.pipeline_setting = {"preset": presets.DEFAULT_PRESET}
            self.pipeline_config = presets.resolve(presets.DEFAULT_PRESET)
        self.capture: CalibrationCapture | None = None
        self.last_calibration: dict[str, Any] | None = None

        self.controller = GripController()
        self.light = False
        self.events: deque[dict] = deque(maxlen=40)

        self.source_info: dict[str, Any] = {"kind": None, "name": None, "connected": False, "message": "Не подключено"}
        self.connected_since = 0.0
        self._msg_times: deque[float] = deque(maxlen=600)
        self.msg_total = 0
        self.unparsed_total = 0
        self.last_raw = ""
        self.last_format = ""
        self.last_data_time = 0.0
        self.imu: tuple[float, ...] | None = None   # последний отсчёт MPU6050 (протокол v0.3)
        self.imu_time = 0.0
        self.imu_total = 0

        self._subscribers: set[asyncio.Queue] = set()
        self._dirty = True
        self._tasks: list[asyncio.Task] = []
        self._load_calibration()

    # ================= жизненный цикл =================
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._broadcast_loop())]

    async def stop(self) -> None:
        self.recorder.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ================= вход =================
    def on_raw(self, data: bytes | bytearray | str, tag: int | None = None) -> None:
        """Вызывается источником на каждое сообщение. tag — номер канала, если источник
        знает его сам (например, у каждого сенсора своя BLE-характеристика)."""
        now = time.monotonic()
        self.msg_total += 1
        self._msg_times.append(now)
        self.last_raw = describe_raw(data)
        self._dirty = True
        head = (data if isinstance(data, str) else bytes(data)[:64].decode("utf-8", "ignore")).lstrip()[:1]
        if head == "#":  # отладочный вывод прошивки
            return
        frames = parse_message(data, prefer_values=self.prefer_values)
        if not frames:
            self.unparsed_total += 1
            return
        for frame in frames:
            if tag is not None and frame.kind == "values" and frame.n == 1:
                frame = Frame(kind="tagged", tags={tag: frame.values[0]}, fmt=frame.fmt + "+char")  # type: ignore[index]
            self._on_frame(frame, now)

    def _on_frame(self, frame: Frame, now: float) -> None:
        self.last_format = frame.fmt
        self.last_data_time = now
        if frame.kind in ("values", "tagged"):
            if frame.kind == "values" and frame.values and any(v not in (0.0, 1.0) for v in frame.values):
                self.prefer_values = True
            if frame.kind == "tagged":
                self.prefer_values = True
            self._set_mode("values")
            if frame.kind == "values":
                self._ensure_channels(frame.n)
                for i, v in enumerate(frame.values or ()):
                    self._update_channel(i, v, now)
            else:
                for tag, v in frame.tags.items():
                    if tag not in self.tag_order and len(self.tag_order) < MAX_CHANNELS:
                        # новый сенсор: вставляем канал на место по порядку меток
                        self.tag_order = sorted(self.tag_order + [tag])
                        self.channels.insert(self.tag_order.index(tag), ChannelProcessor(self.pipeline_config))
                        self._load_calibration()
                    if tag in self.tag_order:
                        self._update_channel(self.tag_order.index(tag), v, now)
            if frame.imu is not None:
                self.imu, self.imu_time = frame.imu, now
                self.imu_total += 1
                self._update_orientation(frame.imu, now)
            self.segmenter.update([c.filtered for c in self.channels], now)
            if self.recorder.active:
                self.recorder.write([c.raw for c in self.channels], now, frame.imu)
            self._set_bits(tuple(c.bit for c in self.channels), now)
        else:
            self._set_mode("bits")
            self._ensure_channels(frame.n)
            self._set_bits(frame.bits or (), now)

    def _update_orientation(self, raw: tuple[float, ...], now: float) -> None:
        """IMU → ориентация. Первый отсчёт после подключения запускает 2 с калибровки гироскопа и обнуление."""
        tr = self.imu_tracker
        if tr.count == 0:
            tr.start_capture(now, zero_after=True)
        was_capturing = tr.capture is not None
        tr.update(raw, now)
        if was_capturing and tr.capture is None:
            self._event("imu", tr.message, source="system")

    def _orientation_live(self, now: float) -> bool:
        return self.imu_tracker.count > 0 and now - self.imu_time <= 0.5

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.channels = []
            self.tag_order = []
            self.bits = ()
            self.commands.reset()
            self.segmenter.reset()

    def _ensure_channels(self, n: int) -> None:
        while len(self.channels) < min(n, MAX_CHANNELS):
            ch = ChannelProcessor(self.pipeline_config)
            cal = self.storage.get("calibration", {})
            i = len(self.channels)
            if i < len(cal.get("open", [])) and i < len(cal.get("fist", [])):
                ch.cal.open, ch.cal.fist = cal["open"][i], cal["fist"][i]
            self.channels.append(ch)

    def _update_channel(self, i: int, value: float, now: float) -> None:
        ch = self.channels[i]
        ch.update(value, now)
        if self.capture is not None and ch.filtered is not None:
            self.capture.add(i, ch.filtered)

    def _set_bits(self, bits: tuple[int, ...], now: float) -> None:
        if bits != self.bits:
            self.bits = bits
            self.combo_since = now

    def on_source_status(self, **info: Any) -> None:
        was = self.source_info.get("connected")
        self.source_info.update(info)
        if self.source_info.get("connected") and not was:
            self.connected_since = time.monotonic()
        if not self.source_info.get("connected"):
            self._set_mode("none")
            self.prefer_values = False
            self.imu = None
            self.imu_tracker.reset()
        self._dirty = True

    # ================= такт: жесты, контроллер, калибровка =================
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        # 1. связь
        link_ok = self.source_info.get("connected", False) and now - self.last_data_time <= LINK_TIMEOUT_S
        msg = self.controller.set_link(bool(link_ok))
        if msg:
            self._event("link", msg, source="system")
        # 2. захват: желаемое сжатие = средний сгиб пальцев с ролью «сжатие» (D-21)
        roles = self.roles()
        live = [self._bend(i, now) for i in range(len(self.channels)) if roles[i].grip]
        live = [b for b in live if b is not None]
        self.grip = sum(live) / len(live) if live else 0.0
        before = self.controller.command
        self.controller.step(self.grip, now)
        if abs(self.controller.command - before) > 1e-4:
            self._dirty = True
        # 2б. поворот захвата: крен кисти относительно исходной позы (D-25)
        before_rot = self.controller.rot_command
        roll = self.imu_tracker.wrist_roll_deg if self._orientation_live(now) and self.imu_tracker.zeroed else None
        self.controller.step_rotation(roll, now)
        if abs(self.controller.rot_command - before_rot) > 1e-3:
            self._dirty = True
        # 3. калибровка
        if self.capture is not None and self.capture.done(now):
            self._finish_calibration()
        # 4. жест-команда (dsp/recognizers/combo.py): роли пальцев, удержание, защита от сопутствующего сжатия
        self.commands.roles = roles
        armed = self.controller.armed and self.controller.link_ok
        sample = Sample(t=now, bits=self.bits, grip=self.grip,
                        context={"armed": armed, "bindings": self.storage.bindings(), "debounce_s": self.debounce_ms / 1000})
        for ev in self.commands.update(sample):
            self._dirty = True
            combo = ev.data["combo"]
            if ev.kind == "command_ignored":
                self._event("ignored", f"Жест {combo} не выполнен: пальцы команд двигались вместе со сжатием",
                            source="system", combo=combo)
            elif ev.data["action"] in ACTIONS:
                self.toggle_action(ev.data["action"], source="gesture", combo=combo)

    def _bend(self, i: int, now: float) -> float | None:
        if self.mode == "bits":
            return float(self.bits[i]) if i < len(self.bits) else None
        ch = self.channels[i]
        return None if ch.stale(now) else ch.bend

    # ================= действия =================
    def toggle_action(self, action_id: str, source: str = "manual", combo: str | None = None) -> None:
        if action_id == "arm":
            if not self.controller.armed and not self.controller.link_ok:
                self._event(action_id, "Нельзя разрешить управление: нет связи с перчаткой", source=source, combo=combo)
                return
            self.controller.armed = not self.controller.armed
            on = self.controller.armed
        elif action_id == "hold":
            self.controller.hold = not self.controller.hold
            on = self.controller.hold
        else:
            self.light = not self.light
            on = self.light
        a = ACTIONS[action_id]
        self._event(action_id, f"{a.title}: {(a.on_label if on else a.off_label).lower()}", source=source, combo=combo, on=on)

    def reset_actions(self) -> None:
        self.controller.armed = self.controller.hold = False
        self.controller.command = 0.0
        self.light = False
        self._event("reset", "Пульт сброшен: управление заблокировано, захват открыт", source="manual")

    def action_state(self) -> dict[str, bool]:
        return {"arm": self.controller.armed, "hold": self.controller.hold, "light": self.light}

    def _event(self, action: str, text: str, source: str, combo: str | None = None, on: bool | None = None) -> None:
        self.events.appendleft({"ts": time.time(), "action": action, "text": text, "source": source, "combo": combo, "on": on})
        self._dirty = True

    # ================= калибровка =================
    def start_calibration(self, target: str, duration: float = 1.5) -> None:
        if target not in ("open", "fist"):
            raise ValueError("target должен быть open или fist")
        if self.mode != "values" or not self.channels:
            raise RuntimeError("Калибровка возможна только при потоке значений АЦП")
        self.capture = CalibrationCapture(target, len(self.channels), time.monotonic(), duration)
        self._dirty = True

    def _finish_calibration(self) -> None:
        assert self.capture is not None
        target, result = self.capture.target, self.capture.result()
        self.capture = None
        cal = self.storage.get("calibration", {"open": [], "fist": []})
        cal.setdefault(target, [])
        cal[target] = [r if r is not None else (cal[target][i] if i < len(cal[target]) else None)
                       for i, r in enumerate(result)]
        self.storage.put("calibration", cal)
        self._load_calibration()
        valid = sum(1 for c in self.channels if c.cal.valid)
        self.last_calibration = {"target": target, "values": result, "valid_channels": valid}
        self._event("calibration", f"Калибровка «{'ладонь' if target == 'open' else 'кулак'}» записана; "
                    f"откалибровано каналов: {valid} из {len(self.channels)}", source="system")

    def _load_calibration(self) -> None:
        cal = self.storage.get("calibration", {})
        for i, ch in enumerate(self.channels):
            o, f = cal.get("open", []), cal.get("fist", [])
            ch.cal.open = o[i] if i < len(o) else None
            ch.cal.fist = f[i] if i < len(f) else None

    def reset_calibration(self) -> None:
        self.storage.put("calibration", {"open": [], "fist": []})
        for ch in self.channels:
            ch.cal.open = ch.cal.fist = None
            ch.auto_rest, ch.auto_max_dev, ch._rest_samples = None, 0.0, []
        self._event("calibration", "Калибровка сброшена — снова автокалибровка", source="system")

    # ================= запись =================
    def start_recording(self) -> str:
        path = self.recorder.start(len(self.channels) or 5, imu=self._imu_live(time.monotonic()))
        self._event("record", f"Запись начата: {path.name}", source="manual")
        return path.name

    def stop_recording(self) -> str | None:
        rows = self.recorder.rows
        path = self.recorder.stop()
        if path:
            self._event("record", f"Запись остановлена: {path.name}, строк {rows}", source="manual")
        return path.name if path else None

    # ================= настройки =================
    def set_binding(self, combo: str, action_id: str | None) -> None:
        self.storage.set_binding(combo, action_id)
        self._dirty = True

    @property
    def committed(self) -> str | None:
        """Последняя комбинация, по которой принято решение (для интерфейса и тестов)."""
        return self.commands.committed

    def roles(self) -> list[Role]:
        """Роли каналов (сжатие / команды) для текущего числа каналов."""
        return normalize_roles(self._stored_roles, max(len(self.channels), len(self.bits)))

    def set_pipeline(self, preset: str, config: list[dict] | None = None) -> list[dict]:
        """Выбрать конвейер фильтров (пресет или "custom" + описание JSON) для всех каналов.
        Неверное описание — ValueError (настройка не меняется). Калибровка сохраняется."""
        cfg = presets.resolve(preset, config)
        self.pipeline_setting = {"preset": preset, "config": cfg} if preset == "custom" else {"preset": preset}
        self.pipeline_config = cfg
        self.storage.put("pipeline", self.pipeline_setting)
        for ch in self.channels:
            ch.set_pipeline(cfg)
        self._event("pipeline", f"Фильтрация: {self.pipeline_title()}", source="manual")
        return cfg

    def imu_zero(self) -> None:
        if not self.imu_tracker.count:
            raise RuntimeError("Нет данных IMU — обнулять нечего")
        self.imu_tracker.set_zero()
        self._event("imu", "Положение обнулено: текущая поза — исходная", source="manual")

    def imu_calibrate(self) -> None:
        if not self.imu_tracker.count:
            raise RuntimeError("Нет данных IMU — калибровать нечего")
        self.imu_tracker.start_capture(time.monotonic())
        self._dirty = True

    def pipeline_title(self) -> str:
        preset = self.pipeline_setting.get("preset")
        return presets.PRESETS[preset]["title"] if preset in presets.PRESETS else "свой конвейер"

    def set_settings(self, finger_map: list[str] | None = None, debounce_ms: int | None = None,
                     channel_roles: list[dict] | None = None, imu_mount: str | None = None,
                     hand: str | None = None) -> None:
        if imu_mount is not None:
            self.imu_tracker.set_mount(imu_mount)          # ValueError — неизвестный пресет
            self.storage.put("imu_mount", imu_mount)
            self.imu_tracker.reset()                       # прежняя поза в других осях бессмысленна
        if hand is not None:
            if hand not in ("right", "left"):
                raise ValueError("hand: right или left")
            self.hand = hand
            self.storage.put("hand", hand)
        if channel_roles is not None:
            current = [r.as_dict() for r in normalize_roles(self._stored_roles, max(len(channel_roles), 8))]
            current[: len(channel_roles)] = [{"grip": bool(r.get("grip")), "cmd": bool(r.get("cmd"))} for r in channel_roles]
            self._stored_roles = current
            self.storage.put("channel_roles", current)
        if finger_map is not None:
            self.finger_map = finger_map
            self.storage.put("finger_map", finger_map)
        if debounce_ms is not None:
            self.debounce_ms = debounce_ms
            self.storage.put("debounce_ms", debounce_ms)
        self._dirty = True

    # ================= состояние для интерфейса =================
    def rate(self, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        return round(sum(1 for t in self._msg_times if now - t <= 2.0) / 2.0, 1)

    def warnings(self, now: float | None = None) -> list[str]:
        """Предупреждения о качестве потока для «Пульта» и «Отладки потока»."""
        now = time.monotonic() if now is None else now
        if not self.source_info.get("connected") or self.source_info.get("kind") == "replay":
            return []
        if now - self.connected_since < WARN_GRACE_S:
            return []
        out = []
        rate = self.rate(now)
        if self.msg_total == 0 or now - self.last_data_time > 1.0:
            out.append("Данные не приходят больше 1 с. Проверьте питание перчатки; если поток идёт только при движении "
                       "пальцев — прошивка шлёт значения лишь при изменении, нужна непрерывная отправка 50 Гц.")
        elif rate < MIN_RATE_HZ:
            out.append(f"Частота потока {rate:g} сообщ./с — меньше {MIN_RATE_HZ:g}. Захват будет двигаться рывками. "
                       "Ожидается 50 Гц непрерывно; возможно, прошивка шлёт только изменения или мешают помехи BLE.")
        if self.mode == "values":
            for i, ch in enumerate(self.channels):
                if ch.stale(now) and ch.count:
                    out.append(f"S{i + 1}: канал молчит больше 1 с (нет данных по этому сенсору).")
                elif ch.flat(now):
                    out.append(f"S{i + 1}: значение {ch.raw:g} не меняется 3 с. Живой сенсор всегда немного шумит — "
                               "проверьте провод и резистор делителя (или прошивка шлёт только изменения).")
        return out

    def _imu_live(self, now: float) -> bool:
        return self.imu is not None and now - self.imu_time <= 1.0

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        n = len(self.channels)
        channels = []
        for i in range(n):
            ch = self.channels[i]
            channels.append({
                "raw": ch.raw if self.mode == "values" else None,
                "filtered": round(ch.filtered, 1) if ch.filtered is not None else None,
                "bend": round(self._bend(i, now) or 0.0, 3),
                "bit": self.bits[i] if i < len(self.bits) else 0,
                "stale": ch.stale(now) if self.mode == "values" else False,
                "mode": ch.mode if self.mode == "values" else "bits",
                "cal": {"open": ch.cal.open, "fist": ch.cal.fist},
            })
        bindings = {c: a for c, a in self.storage.bindings().items() if a in ACTIONS}
        c = self.controller
        return {
            "source": self.source_info,
            "mode": self.mode,
            "n_channels": n,
            "channels": channels,
            "combo": "".join(str(b) for b in self.bits) if self.bits else None,
            "committed": self.commands.committed,
            "roles": [r.as_dict() for r in self.roles()],
            "grip": round(self.grip, 3),
            "motion": self.segmenter.moving,
            "orientation": self.imu_tracker.snapshot(now) if self.imu_tracker.count else None,
            "hand": self.hand,
            "pipeline": {"preset": self.pipeline_setting.get("preset"), "title": self.pipeline_title(),
                         "config": self.pipeline_config},
            "command": {
                "armed_mode": self.commands.armed_mode,
                "candidate": self.commands.candidate,
                "progress": round(self.commands.progress(now), 2),
                "blocked": self.commands.blocked,
                "hold_s": self.commands.last_hold_s,
                "armed_bindings": armed_bindings(bindings, self.roles()),
            },
            "finger_map": self.finger_map,
            "debounce_ms": self.debounce_ms,
            "bindings": bindings,
            "actions": self.action_state(),
            "controller": {"state": c.state, "target": round(c.target, 3), "command": round(c.command, 3),
                           "max_close": c.config.max_close, "armed": c.armed, "hold": c.hold, "link": c.link_ok,
                           "rot_target": round(c.rot_target, 1), "rot_command": round(c.rot_command, 1),
                           "rot_limit": c.config.rot_limit_deg},
            "calibrating": self.capture.target if self.capture else None,
            "last_calibration": self.last_calibration,
            "recording": {"active": self.recorder.active, "file": self.recorder.path.name if self.recorder.path else None,
                          "rows": self.recorder.rows},
            "events": list(self.events)[:20],
            "stats": {"rate": self.rate(), "total": self.msg_total, "unparsed": self.unparsed_total,
                      "last_raw": self.last_raw, "format": self.last_format},
            "warnings": self.warnings(now),
            "imu": ({**dict(zip(IMU_FIELDS, self.imu)), "stale": not self._imu_live(now), "total": self.imu_total}
                    if self.imu is not None else None),
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.add(q)
        self._dirty = True
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast_loop(self) -> None:
        while True:
            await asyncio.sleep(1 / BROADCAST_HZ)
            if not self._subscribers:
                continue
            if not self._dirty and self.mode == "none":
                continue
            self._dirty = False
            snap = self.snapshot()
            for q in list(self._subscribers):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(snap)
