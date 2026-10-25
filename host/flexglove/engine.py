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
from .control import LINK_TIMEOUT_S, GripController
from .parser import Frame, describe_raw, parse_message
from .processing import CalibrationCapture, ChannelProcessor
from .recording import Recorder
from .storage import Storage

FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
DEFAULT_FINGER_MAP = ["thumb", "index", "middle", "ring", "pinky"]  # канал i -> палец
DEFAULT_DEBOUNCE_MS = 200
BROADCAST_HZ = 30
TICK_S = 0.02
MAX_CHANNELS = 8


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
        self.committed: str | None = None
        self.capture: CalibrationCapture | None = None
        self.last_calibration: dict[str, Any] | None = None

        self.controller = GripController()
        self.light = False
        self.events: deque[dict] = deque(maxlen=40)

        self.source_info: dict[str, Any] = {"kind": None, "name": None, "connected": False, "message": "Не подключено"}
        self._msg_times: deque[float] = deque(maxlen=600)
        self.msg_total = 0
        self.unparsed_total = 0
        self.last_raw = ""
        self.last_format = ""
        self.last_data_time = 0.0

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
                        self.channels.insert(self.tag_order.index(tag), ChannelProcessor())
                        self._load_calibration()
                    if tag in self.tag_order:
                        self._update_channel(self.tag_order.index(tag), v, now)
            if self.recorder.active:
                self.recorder.write([c.raw for c in self.channels], now)
            self._set_bits(tuple(c.bit for c in self.channels), now)
        else:
            self._set_mode("bits")
            self._ensure_channels(frame.n)
            self._set_bits(frame.bits or (), now)

    def _set_mode(self, mode: str) -> None:
        if mode != self.mode:
            self.mode = mode
            self.channels = []
            self.tag_order = []
            self.bits = ()
            self.committed = None

    def _ensure_channels(self, n: int) -> None:
        while len(self.channels) < min(n, MAX_CHANNELS):
            ch = ChannelProcessor()
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
        self.source_info.update(info)
        if not self.source_info.get("connected"):
            self._set_mode("none")
            self.prefer_values = False
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
        # 2. захват: желаемое сжатие = средний сгиб пальцев с данными
        live = [self._bend(i, now) for i in range(len(self.channels))]
        live = [b for b in live if b is not None]
        before = self.controller.command
        self.controller.step(sum(live) / len(live) if live else 0.0, now)
        if abs(self.controller.command - before) > 1e-4:
            self._dirty = True
        # 3. калибровка
        if self.capture is not None and self.capture.done(now):
            self._finish_calibration()
        # 4. жест-комбинация: фиксируется после удержания debounce_ms
        if not self.bits:
            return
        combo = "".join(str(b) for b in self.bits)
        if combo == self.committed or (now - self.combo_since) * 1000 < self.debounce_ms:
            return
        self.committed = combo
        self._dirty = True
        action_id = self.storage.bindings().get(combo)
        if action_id in ACTIONS:
            self.toggle_action(action_id, source="gesture", combo=combo)

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
        path = self.recorder.start(len(self.channels) or 5)
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

    def set_settings(self, finger_map: list[str] | None = None, debounce_ms: int | None = None) -> None:
        if finger_map is not None:
            self.finger_map = finger_map
            self.storage.put("finger_map", finger_map)
        if debounce_ms is not None:
            self.debounce_ms = debounce_ms
            self.storage.put("debounce_ms", debounce_ms)
        self._dirty = True

    # ================= состояние для интерфейса =================
    def rate(self) -> float:
        now = time.monotonic()
        return round(sum(1 for t in self._msg_times if now - t <= 2.0) / 2.0, 1)

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
            "committed": self.committed,
            "finger_map": self.finger_map,
            "debounce_ms": self.debounce_ms,
            "bindings": bindings,
            "actions": self.action_state(),
            "controller": {"state": c.state, "target": round(c.target, 3), "command": round(c.command, 3),
                           "max_close": c.config.max_close, "armed": c.armed, "hold": c.hold, "link": c.link_ok},
            "calibrating": self.capture.target if self.capture else None,
            "last_calibration": self.last_calibration,
            "recording": {"active": self.recorder.active, "file": self.recorder.path.name if self.recorder.path else None,
                          "rows": self.recorder.rows},
            "events": list(self.events)[:20],
            "stats": {"rate": self.rate(), "total": self.msg_total, "unparsed": self.unparsed_total,
                      "last_raw": self.last_raw, "format": self.last_format},
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
