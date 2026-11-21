"""Ядро приложения.

Принимает сообщения от источника → разбирает (parser) → обрабатывает каждый канал (processing)
→ получает непрерывные сгибы → обучает/распознаёт статические позы → ставит локальное аудио в очередь.
Рассылает состояние всем вкладкам. Архивный контроллер сохранён, автомат команд отключён (D-35).

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
from .audio import AudioPlayer
from .gestures import GestureSystem
from .calib_wizard import CalibrationWizard
from .dsp.recognizers import CommandRecognizer, Role, armed_bindings, normalize_roles
from .control import LINK_TIMEOUT_S, GripController
from .parser import IMU_FIELDS, Frame, describe_raw, parse_message
from .dsp import presets
from .dsp.segmentation import HandSegmenter
from .imu import DEFAULT_MOUNT, MOUNT_PRESETS, ImuTracker
from .processing import CalibrationCapture, ChannelProcessor
from .recording import Recorder
from .storage import Storage
from .sources.mock import REST, BEND_DELTA

FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
DEFAULT_FINGER_MAP = ["thumb", "index", "middle", "ring", "pinky"]  # канал i -> палец
DEFAULT_DEBOUNCE_MS = 200
BROADCAST_HZ = 30
TICK_S = 0.02
MAX_CHANNELS = 8
MIN_RATE_HZ = 20.0          # ниже — предупреждение: управление станет рывками (прошивка шлёт 50 Гц)
WARN_GRACE_S = 2.5          # первые секунды после подключения не предупреждаем (частота ещё набирается)


class Engine:
    def __init__(self, storage: Storage, data_dir: Path | None = None, *, clock=None, audio=None):
        self.storage = storage
        self.clock = clock or (lambda: time.monotonic())
        self.gestures = GestureSystem(storage)
        self.audio = audio or AudioPlayer(Path(__file__).resolve().parents[2] / "audio")
        self._gesture_context = None
        self._gesture_input_ok = False
        self.finger_map: list[str] = storage.get("finger_map", DEFAULT_FINGER_MAP)
        if len(self.finger_map) < 5:  # настройки этапа 0 хранили 2 пальца
            self.finger_map = (self.finger_map + [f for f in DEFAULT_FINGER_MAP if f not in self.finger_map])[:5]
        self.debounce_ms: int = storage.get("debounce_ms", DEFAULT_DEBOUNCE_MS)
        self.recorder = Recorder((data_dir or Path("data")) / "recordings")

        self.mode = "none"                 # none | values | bits
        self.prefer_values = False
        self.channels: list[ChannelProcessor] = []
        self.tag_order: list[int] = []     # метки каналов в порядке номеров
        self._channel_layout: str | None = None
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
        self.wizard = CalibrationWizard()          # мастер калибровки «ладонь → кулак → итог» (этап 4б)
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
        self.audio.start()
        self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._broadcast_loop())]

    async def stop(self) -> None:
        self.recorder.stop()
        await asyncio.to_thread(self.audio.close)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ================= вход =================
    def on_raw(self, data: bytes | bytearray | str, tag: int | None = None) -> None:
        """Вызывается источником на каждое сообщение. tag — номер канала, если источник
        знает его сам (например, у каждого сенсора своя BLE-характеристика)."""
        now = self.clock()
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
            if self._channel_layout is not None and self._channel_layout != frame.kind:
                # Метки и позиционные номера — разные соответствия, даже при одинаковом числе каналов.
                self.channels = []
                self.tag_order = []
                self.bits = ()
            self._channel_layout = frame.kind
            if frame.kind == "values":
                if len(self.channels) > frame.n:
                    self.channels = self.channels[:frame.n]
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
            self._channel_layout = None
            self.bits = ()
            self.commands.reset()
            self.segmenter.reset()

    def _ensure_channels(self, n: int) -> None:
        while len(self.channels) < min(n, MAX_CHANNELS):
            ch = ChannelProcessor(self.pipeline_config)
            self.channels.append(ch)
            self._load_calibration()

    def _update_channel(self, i: int, value: float, now: float) -> None:
        ch = self.channels[i]
        ch.update(value, now)
        if self.capture is not None and ch.filtered is not None:
            self.capture.add(i, ch.filtered)
        if self.wizard.capture is not None:
            self.wizard.add(i, ch.raw, ch.filtered)

    def _set_bits(self, bits: tuple[int, ...], now: float) -> None:
        if bits != self.bits:
            self.bits = bits
            self.combo_since = now

    def on_source_status(self, **info: Any) -> None:
        was = self.source_info.get("connected")
        previous_kind = self.source_info.get("kind")
        self.source_info.update(info)
        if self.source_info.get("connected") and not was:
            self.connected_since = self.clock()
        if not self.source_info.get("connected") or previous_kind != self.source_info.get("kind") or not was:
            self.audio.stop()
            self.gestures.suspend("Источник отключён или переподключён: сначала выйдите из текущей позы")
            self._gesture_context = None
            self._gesture_input_ok = False
            self._set_mode("none")
            self.capture = None
            self.wizard.reset()
            self.last_calibration = None
            self.channels = []
            self.tag_order = []
            self.bits = ()
            self.commands.reset()
            self.segmenter.reset()
            self._msg_times.clear()
            self.msg_total = self.unparsed_total = self.imu_total = 0
            self.last_data_time = self.imu_time = 0.0
            self.last_raw = self.last_format = ""
            self.grip = 0.0
            self.controller.set_link(False)
            self.prefer_values = False
            self.imu = None
            self.imu_tracker.reset()
        self._dirty = True

    # ================= такт: жесты, контроллер, калибровка =================
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            self.tick(self.clock())

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
        if self.wizard.update(now):
            self._dirty = True
            if self.wizard.step == "review":
                bad = [i + 1 for i, r in enumerate(self.wizard.summary()) if r["grade"] in ("flat", "no_data")]
                self._event("calibration", "Калибровка: замеры готовы — проверьте итог и нажмите «Сохранить»"
                            + (f"; каналы без сигнала: {', '.join(f'S{i}' for i in bad)}" if bad else ""), source="system")
        # D-35: прежние ARM/hold/light не распознаются параллельно с аудиожестами.
        self._tick_gestures(now)

    def gesture_input(self, now: float) -> tuple[list[float] | None, dict | None, float | None, str]:
        kind = self.source_info.get("kind")
        profile = "demo" if kind == "mock" else "replay" if kind == "replay" else "glove"
        context = {"profile": profile, "channels": [
            {"id": f"tag:{self.tag_order[i]}" if i < len(self.tag_order) else f"slot:{i}",
             "finger": self.finger_map[i] if i < len(self.finger_map) else None,
             "calibration": {"open": ch.cal.open, "fist": ch.cal.fist}}
            for i, ch in enumerate(self.channels)], "hand": self.hand,
            "pipeline": self.pipeline_config, "normalization": "fixed-linear-v1"}
        if not self.source_info.get("connected") or self.mode != "values" or not self.channels:
            return None, context, None, "Нужен подключённый источник непрерывных значений АЦП"
        if self.capture or self.wizard.capture or self.wizard.step != "idle" or self.imu_tracker.capture is not None:
            return None, context, None, "Во время калибровки распознавание и обучение запрещены"
        if not all(ch.cal.valid for ch in self.channels):
            return None, context, None, "Нужна сохранённая калибровка всех каналов; автокалибровка меняет шкалу"
        if any(now - ch.last_update > self.gestures.thresholds.fresh_s or not ch.count for ch in self.channels):
            return None, context, None, "Недостаточно свежих данных всех каналов"
        return [ch.bend for ch in self.channels], context, min(ch.last_update for ch in self.channels), ""

    def _tick_gestures(self, now: float) -> None:
        values, context, sample_time, reason = self.gesture_input(now)
        if context != self._gesture_context or (values is None and self._gesture_input_ok):
            self.audio.stop()
        self._gesture_context = context
        self._gesture_input_ok = values is not None
        ev = self.gestures.update(values, context, now, sample_time, reason)
        if ev:
            gesture = next(t for t in self.gestures.templates if t["id"] == ev["gesture_id"])
            self._event("gesture_recognized", f"Распознан жест «{gesture['name']}» (событие {ev['sequence']})", "gesture")
            result = self.audio.enqueue(gesture["id"], gesture.get("audio"))
            self._event("audio_queue", f"Аудио: {result['status']} {result['message']}", "system")
        self._dirty = True

    def train_gesture(self, name: str, template_id: str | None = None) -> None:
        now = self.clock()
        values, context, _, reason = self.gesture_input(now)
        if values is None:
            raise ValueError(reason)
        self.audio.stop()
        self.gestures.context = context
        self.gestures.begin(name, now, context, template_id)

    def edit_gesture(self, gesture_id: str, *, name: str | None = None, audio: str | None = None,
                     assign_audio: bool = False, delete: bool = False) -> dict:
        if self.gestures.training:
            raise ValueError("Дождитесь окончания обучения")
        gesture = next((t for t in self.gestures.templates if t["id"] == gesture_id), None)
        if gesture is None:
            raise ValueError("Жест не найден")
        updated = dict(gesture)
        if name is not None:
            name = name.strip()
            if not name or len(name) > 80:
                raise ValueError("Название должно содержать от 1 до 80 символов")
            updated["name"] = name
        if assign_audio:
            if audio is not None:
                self.audio.resolve(audio)
            updated["audio"] = audio
        if delete:
            self.storage.delete_gesture(gesture_id)
        else:
            self.storage.save_gesture(updated)
        self.audio.stop()
        self.gestures.reload()
        return updated

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
    def _require_real_calibration(self) -> None:
        if self.source_info.get("kind") == "mock":
            raise RuntimeError("В демо используются известные диапазоны генератора")

    def start_calibration(self, target: str, duration: float = 1.5) -> None:
        self._require_real_calibration()
        if target not in ("open", "fist"):
            raise ValueError("target должен быть open или fist")
        if self.mode != "values" or not self.channels:
            raise RuntimeError("Калибровка возможна только при потоке значений АЦП")
        self.capture = CalibrationCapture(target, len(self.channels), self.clock(), duration)
        self._dirty = True

    def _finish_calibration(self) -> None:
        self._require_real_calibration()
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

    # ---------- мастер калибровки ----------
    def wizard_start(self, target: str, duration: float | None = None) -> None:
        self._require_real_calibration()
        if self.mode != "values" or not self.channels:
            raise RuntimeError("Калибровка возможна только при потоке значений АЦП — подключите перчатку")
        kw = {} if duration is None else {"duration": duration}
        self.wizard.start(target, len(self.channels), self.clock(), **kw)
        self._dirty = True

    def wizard_save(self) -> dict:
        self._require_real_calibration()
        opens, fists = self.wizard.levels()
        self.storage.put("calibration", {"open": opens, "fist": fists})
        self._load_calibration()
        valid = sum(1 for c in self.channels if c.cal.valid)
        self.last_calibration = {"target": "wizard", "values": fists, "valid_channels": valid}
        self.wizard.reset()
        self._event("calibration", f"Калибровка сохранена: откалибровано каналов {valid} из {len(self.channels)}",
                    source="system")
        return {"valid_channels": valid}

    def wizard_cancel(self) -> None:
        self.wizard.reset()
        self._dirty = True

    def _load_calibration(self) -> None:
        cal = ({"open": [REST[i % 5] for i in range(MAX_CHANNELS)],
                "fist": [REST[i % 5] + BEND_DELTA[i % 5] for i in range(MAX_CHANNELS)]}
               if self.source_info.get("kind") == "mock" else self.storage.get("calibration", {}))
        for i, ch in enumerate(self.channels):
            o, f = cal.get("open", []), cal.get("fist", [])
            ch.cal.open = o[i] if i < len(o) else None
            ch.cal.fist = f[i] if i < len(f) else None

    def reset_calibration(self) -> None:
        self._require_real_calibration()
        self.storage.put("calibration", {"open": [], "fist": []})
        for ch in self.channels:
            ch.cal.open = ch.cal.fist = None
            ch.auto_rest, ch.auto_max_dev, ch._rest_samples = None, 0.0, []
        self._event("calibration", "Калибровка сброшена — снова автокалибровка", source="system")

    # ================= запись =================
    def start_recording(self) -> str:
        path = self.recorder.start(len(self.channels) or 5, imu=self._imu_live(self.clock()))
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
        self._event("imu", "Поворот обнулён: текущее направление руки — «вперёд» (наклоны — по силе тяжести)", source="manual")

    def imu_calibrate(self) -> None:
        if not self.imu_tracker.count:
            raise RuntimeError("Нет данных IMU — калибровать нечего")
        self.imu_tracker.start_capture(self.clock())
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
        now = self.clock() if now is None else now
        return round(sum(1 for t in self._msg_times if now - t <= 2.0) / 2.0, 1)

    def warnings(self, now: float | None = None) -> list[str]:
        """Предупреждения о качестве потока для «Пульта» и «Отладки потока»."""
        now = self.clock() if now is None else now
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
        now = self.clock()
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
                "rest": round(ch.auto_rest, 1) if ch.auto_rest is not None else None,   # уровень покоя автокалибровки
            })
        bindings = {c: a for c, a in self.storage.bindings().items() if a in ACTIONS}
        c = self.controller
        return {
            "gesture_system": {**self.gestures.snapshot(now),
                "profile": (self.gestures.context or {}).get("profile"),
                "templates": [{**t, "compatible": self.gestures.compatible(t),
                    "audio_available": self.audio.available(t.get("audio"))} for t in self.gestures.templates],
                "legacy_commands_enabled": False},
            "audio": self.audio.snapshot(),
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
            "wizard": self.wizard.snapshot(now),
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
        last_broadcast = 0.0
        while True:
            await asyncio.sleep(1 / BROADCAST_HZ)
            if not self._subscribers:
                continue
            now = self.clock()
            # Даже без перчатки подтверждаем, что WebSocket и приложение живы.
            if not self._dirty and self.mode == "none" and now - last_broadcast < 1.0:
                continue
            last_broadcast = now
            self._dirty = False
            snap = self.snapshot()
            for q in list(self._subscribers):
                if q.full():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                q.put_nowait(snap)
