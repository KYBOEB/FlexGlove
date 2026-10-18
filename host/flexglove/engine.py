"""Ядро: принимает кадры от источника, устраняет дребезг, вызывает привязанные действия
и рассылает состояние подписчикам (WebSocket-клиентам интерфейса)."""
from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any

from .actions import ACTIONS
from .parser import Frame, describe_raw, parse_message
from .storage import Storage

FINGERS = ["thumb", "index", "middle", "ring", "pinky"]
DEFAULT_FINGER_MAP = ["index", "middle", "ring", "pinky", "thumb"]  # канал i -> палец
DEFAULT_DEBOUNCE_MS = 150
BROADCAST_HZ = 30


class Engine:
    def __init__(self, storage: Storage):
        self.storage = storage
        self.finger_map: list[str] = storage.get("finger_map", DEFAULT_FINGER_MAP)
        self.debounce_ms: int = storage.get("debounce_ms", DEFAULT_DEBOUNCE_MS)

        self.n_channels = 2
        self.raw: Frame | None = None
        self.raw_since = 0.0
        self.committed: str | None = None
        self.action_state = {a: False for a in ACTIONS}
        self.events: deque[dict] = deque(maxlen=30)

        self.source_info: dict[str, Any] = {"kind": None, "name": None, "connected": False, "message": "Не подключено"}
        self._msg_times: deque[float] = deque(maxlen=400)
        self.msg_total = 0
        self.unparsed_total = 0
        self.last_raw = ""
        self.last_format = ""

        self._subscribers: set[asyncio.Queue] = set()
        self._dirty = True
        self._tasks: list[asyncio.Task] = []

    # ---------- жизненный цикл ----------
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._tick_loop()), asyncio.create_task(self._broadcast_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---------- вход от источников ----------
    def on_raw(self, data: bytes | bytearray | str) -> None:
        """Вызывается источником на каждое сообщение (в потоке asyncio)."""
        now = time.monotonic()
        self.msg_total += 1
        self._msg_times.append(now)
        self.last_raw = describe_raw(data)
        head = data.lstrip()[:1] if not isinstance(data, str) else data.lstrip()[:1].encode()
        if head == b"#":  # строка-комментарий от прошивки (отладочный вывод)
            self._dirty = True
            return
        frames = parse_message(data, default_n=self.n_channels)
        if not frames:
            self.unparsed_total += 1
            self._dirty = True
            return
        for frame in frames:
            self._on_frame(frame, now)

    def _on_frame(self, frame: Frame, now: float) -> None:
        self.last_format = frame.fmt
        if frame.n != self.n_channels:
            self.n_channels = frame.n
        if self.raw is None or self.raw.bits != frame.bits:
            self.raw_since = now
        self.raw = frame
        self._dirty = True

    def on_source_status(self, **info: Any) -> None:
        self.source_info.update(info)
        if not self.source_info.get("connected"):
            self.raw = None
        self._dirty = True

    # ---------- логика жестов ----------
    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        """Фиксирует комбинацию, если она удерживается не меньше debounce_ms."""
        if self.raw is None:
            return
        combo = self.raw.combo
        if combo == self.committed:
            return
        if (now - self.raw_since) * 1000 < self.debounce_ms:
            return
        self.committed = combo
        self._dirty = True
        action_id = self.storage.bindings().get(combo)
        if action_id and action_id in ACTIONS:
            self.toggle_action(action_id, source="gesture", combo=combo)

    def toggle_action(self, action_id: str, source: str = "manual", combo: str | None = None) -> None:
        self.action_state[action_id] = not self.action_state[action_id]
        action = ACTIONS[action_id]
        self.events.appendleft({
            "ts": time.time(),
            "action": action_id,
            "title": action.title,
            "on": self.action_state[action_id],
            "label": action.on_label if self.action_state[action_id] else action.off_label,
            "source": source,
            "combo": combo,
        })
        self._dirty = True

    def reset_actions(self) -> None:
        for a in self.action_state:
            self.action_state[a] = False
        self._dirty = True

    # ---------- настройки ----------
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

    # ---------- рассылка ----------
    def rate(self) -> float:
        now = time.monotonic()
        recent = [t for t in self._msg_times if now - t <= 2.0]
        return round(len(recent) / 2.0, 1)

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source_info,
            "n_channels": self.n_channels,
            "raw": self.raw.combo if self.raw else None,
            "values": list(self.raw.values) if self.raw and self.raw.values else None,
            "committed": self.committed,
            "finger_map": self.finger_map[: max(self.n_channels, 1)],
            "finger_map_full": self.finger_map,
            "debounce_ms": self.debounce_ms,
            "bindings": self.storage.bindings(),
            "actions": self.action_state,
            "events": list(self.events)[:15],
            "stats": {
                "rate": self.rate(),
                "total": self.msg_total,
                "unparsed": self.unparsed_total,
                "last_raw": self.last_raw,
                "format": self.last_format,
            },
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._subscribers.add(q)
        self._dirty = True
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast_loop(self) -> None:
        last_stats = 0.0
        while True:
            await asyncio.sleep(1 / BROADCAST_HZ)
            now = time.monotonic()
            if now - last_stats >= 1.0:  # частота сообщений обновляется раз в секунду
                last_stats = now
                self._dirty = True
            if not self._dirty or not self._subscribers:
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
