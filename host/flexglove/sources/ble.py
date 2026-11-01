"""Bluetooth Low Energy через bleak (Windows / macOS / Linux).

Чтобы не зависеть от UUID в прошивке, после подключения подписываемся на ВСЕ
характеристики с notify/indicate. Если таких нет — раз в 100 мс читаем доступные
для чтения характеристики (кроме стандартных служебных сервисов).
Если в прошивке есть характеристика DATA из протокола v0.1 — слушаем только её.

При неожиданном обрыве связи источник сам пытается переподключиться RECONNECT_DELAYS
(3 попытки: через 1, 2 и 4 с). Пока связи нет, ядро замораживает захват и снимает ARM (control.py).
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner

from .base import Source

log = logging.getLogger("flexglove.ble")

# UUID из docs/protocol.md (протокол v0.1)
GLOVE_SERVICE_UUID = "6e7a0001-6c6f-7665-2d66-6c6578676c76"
GLOVE_DATA_UUID = "6e7a0002-6c6f-7665-2d66-6c6578676c76"

# Служебные сервисы, которые не опрашиваем: Generic Access, Generic Attribute, Device Information, Battery
SKIP_SERVICES = {"00001800", "00001801", "0000180a", "0000180f"}

RECONNECT_DELAYS = (1.0, 2.0, 4.0)   # паузы перед попытками переподключения, с

HINT_BUSY = ("Проверьте, что перчатка включена и не подключена к телефону или программе Захара "
             "(ESP32 держит только одно подключение).")


def friendly_error(exc: BaseException) -> str:
    """Понятное сообщение вместо технического текста bleak/WinRT."""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in low or "timed out" in low:
        return f"Перчатка не ответила вовремя. {HINT_BUSY}"
    if "not found" in low or "was not found" in low or exc.__class__.__name__ == "BleakDeviceNotFoundError":
        return f"Перчатка не найдена. {HINT_BUSY} Нажмите «Найти устройства» ещё раз."
    if "bluetooth" in low and ("off" in low or "not available" in low or "radio" in low):
        return "Bluetooth на компьютере выключен или недоступен — включите его в параметрах Windows."
    if "unreachable" in low or "disconnected" in low:
        return f"Связь с перчаткой оборвалась при подключении. {HINT_BUSY}"
    return f"{text}. {HINT_BUSY}"


async def scan_ble(timeout: float = 5.0) -> list[dict]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices = []
    for address, (device, adv) in found.items():
        name = device.name or adv.local_name or ""
        devices.append({
            "address": address,
            "name": name,
            "rssi": adv.rssi,
            "services": list(adv.service_uuids or []),
            "is_glove": GLOVE_SERVICE_UUID in [u.lower() for u in (adv.service_uuids or [])],
        })
    # сначала устройства с именем, ближе (сильнее сигнал) — выше
    devices.sort(key=lambda d: (not d["is_glove"], d["name"] == "", -(d["rssi"] or -999)))
    return devices


class BleSource(Source):
    kind = "ble"

    def __init__(self, on_raw, on_status, address: str, name: str | None = None,
                 client_factory=BleakClient, reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS):
        super().__init__(on_raw, on_status)
        self.address = address
        self.name = name or address
        self.client = None
        self._client_factory = client_factory
        self._delays = reconnect_delays
        self._poll_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self.subscribed: list[str] = []
        self.reconnects = 0          # успешных переподключений за сессию

    # ---------------- подключение ----------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self.on_status(kind=self.kind, name=self.name, connected=False, message=f"Подключаюсь к {self.name}…")
        try:
            mode = await self._connect()
        except Exception as exc:  # noqa: BLE001 - показываем понятную причину
            raise ConnectionError(friendly_error(exc)) from exc
        self.on_status(kind=self.kind, name=self.name, connected=True, message=f"BLE: {mode}", characteristics=self.subscribed)

    async def _connect(self) -> str:
        self.subscribed = []
        self.client = self._client_factory(self.address, disconnected_callback=self._on_disconnect, timeout=15)
        await self.client.connect()

        notify_chars, read_chars = [], []
        for service in self.client.services:
            if service.uuid[:8].lower() in SKIP_SERVICES:
                continue
            for char in service.characteristics:
                props = set(char.properties)
                if char.uuid.lower() == GLOVE_DATA_UUID:
                    notify_chars = [char]
                    break
                if props & {"notify", "indicate"}:
                    notify_chars.append(char)
                elif "read" in props:
                    read_chars.append(char)
            else:
                continue
            break  # нашли DATA протокола v0.1

        # Несколько notify-характеристик = у каждого сенсора своя: номер канала — по порядку handle.
        notify_chars.sort(key=lambda c: c.handle)
        many = len(notify_chars) > 1
        for i, char in enumerate(notify_chars):
            await self.client.start_notify(char, self._make_notify(i if many else None))
            self.subscribed.append(char.uuid)

        if notify_chars:
            return f"подписка на {len(notify_chars)} характ."
        if read_chars:
            self._poll_task = asyncio.create_task(self._poll(read_chars))
            return f"опрос {len(read_chars)} характ. (нет notify)"
        return "нет характеристик для чтения"

    def _make_notify(self, tag: int | None):
        def _notify(_char, data: bytearray) -> None:
            self.on_raw(bytes(data), tag=tag)
        return _notify

    async def _poll(self, chars) -> None:
        while self.client and self.client.is_connected:
            for char in chars:
                try:
                    self.on_raw(bytes(await self.client.read_gatt_char(char)), tag=chars.index(char) if len(chars) > 1 else None)
                except Exception:  # noqa: BLE001 - устройство могло отключиться
                    pass
            await asyncio.sleep(0.1)

    # ---------------- обрыв и переподключение ----------------
    def _on_disconnect(self, _client) -> None:
        """bleak может вызвать это не из нашего цикла asyncio — переносим обработку в него."""
        if self._stopping or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._begin_reconnect)

    def _begin_reconnect(self) -> None:
        if self._stopping or (self._reconnect_task and not self._reconnect_task.done()):
            return
        if self._poll_task:
            self._poll_task.cancel()
        self.on_status(kind=self.kind, name=self.name, connected=False,
                       message="Связь с перчаткой потеряна — захват заморожен, управление снято. Переподключаюсь…")
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        total = len(self._delays)
        last_error = ""
        for attempt, delay in enumerate(self._delays, start=1):
            await asyncio.sleep(delay)
            if self._stopping:
                return
            self.on_status(kind=self.kind, name=self.name, connected=False,
                           message=f"Переподключение {attempt} из {total}…")
            try:
                mode = await self._connect()
            except Exception as exc:  # noqa: BLE001
                last_error = friendly_error(exc)
                log.info("BLE reconnect %d/%d failed: %s", attempt, total, exc)
                await self._close_client()
                continue
            self.reconnects += 1
            self.on_status(kind=self.kind, name=self.name, connected=True,
                           message=f"BLE: переподключено (попытка {attempt}), {mode}. Управление снова включается вручную",
                           characteristics=self.subscribed)
            return
        self.on_status(kind=self.kind, name=self.name, connected=False,
                       message=f"Не удалось переподключиться за {total} попытки. {last_error or HINT_BUSY} "
                               "Затем нажмите «Найти устройства» → «Подключить».")

    async def _close_client(self) -> None:
        try:
            if self.client and self.client.is_connected:
                await self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        self._stopping = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
        if self._poll_task:
            self._poll_task.cancel()
        await self._close_client()
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
