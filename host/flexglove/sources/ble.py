"""Bluetooth Low Energy через bleak (Windows / macOS / Linux).

Чтобы не зависеть от UUID в прошивке, после подключения подписываемся на ВСЕ
характеристики с notify/indicate. Если таких нет — раз в 100 мс читаем доступные
для чтения характеристики (кроме стандартных служебных сервисов).
Если в прошивке есть характеристика DATA из протокола v0.1 — слушаем только её.
"""
from __future__ import annotations

import asyncio

from bleak import BleakClient, BleakScanner

from .base import Source

# UUID из docs/protocol.md (протокол v0.1)
GLOVE_SERVICE_UUID = "6e7a0001-6c6f-7665-2d66-6c6578676c76"
GLOVE_DATA_UUID = "6e7a0002-6c6f-7665-2d66-6c6578676c76"

# Служебные сервисы, которые не опрашиваем: Generic Access, Generic Attribute, Device Information, Battery
SKIP_SERVICES = {"00001800", "00001801", "0000180a", "0000180f"}


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

    def __init__(self, on_raw, on_status, address: str, name: str | None = None):
        super().__init__(on_raw, on_status)
        self.address = address
        self.name = name or address
        self.client: BleakClient | None = None
        self._poll_task: asyncio.Task | None = None
        self.subscribed: list[str] = []

    def _on_disconnect(self, _client: BleakClient) -> None:
        self.on_status(kind=self.kind, name=self.name, connected=False, message="Устройство отключилось")

    def _make_notify(self, tag: int | None):
        def _notify(_char, data: bytearray) -> None:
            self.on_raw(bytes(data), tag=tag)
        return _notify

    async def start(self) -> None:
        self.on_status(kind=self.kind, name=self.name, connected=False, message=f"Подключаюсь к {self.name}…")
        self.client = BleakClient(self.address, disconnected_callback=self._on_disconnect, timeout=15)
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
            mode = f"подписка на {len(notify_chars)} характ."
        elif read_chars:
            self._poll_task = asyncio.create_task(self._poll(read_chars))
            mode = f"опрос {len(read_chars)} характ. (нет notify)"
        else:
            mode = "нет характеристик для чтения"
        self.on_status(kind=self.kind, name=self.name, connected=True, message=f"BLE: {mode}", characteristics=self.subscribed)

    async def _poll(self, chars) -> None:
        while self.client and self.client.is_connected:
            for char in chars:
                try:
                    self.on_raw(bytes(await self.client.read_gatt_char(char)), tag=chars.index(char) if len(chars) > 1 else None)
                except Exception:  # noqa: BLE001 - устройство могло отключиться
                    pass
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        if self.client and self.client.is_connected:
            await self.client.disconnect()
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
