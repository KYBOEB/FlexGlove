"""Последовательный порт: USB-кабель или Bluetooth Classic (SPP).

На macOS ESP32 с BluetoothSerial после сопряжения появляется как /dev/cu.<имя>,
на Windows — как COM-порт. Каждая строка — одно сообщение.
"""
from __future__ import annotations

import asyncio
import threading

import serial
from serial.tools import list_ports

from .base import Source


def list_serial_ports() -> list[dict]:
    ports = []
    for p in list_ports.comports():
        ports.append({"port": p.device, "description": p.description or "", "hwid": p.hwid or ""})
    return ports


class SerialSource(Source):
    kind = "serial"

    def __init__(self, on_raw, on_status, port: str, baud: int = 115200):
        super().__init__(on_raw, on_status)
        self.port = port
        self.baud = baud
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ser: serial.Serial | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.on_status(kind=self.kind, name=self.port, connected=False, message=f"Открываю {self.port}…")
        # открываем порт в отдельном потоке, чтобы не блокировать сервер
        self._ser = await asyncio.to_thread(serial.Serial, self.port, self.baud, timeout=0.2)
        self.on_status(kind=self.kind, name=self.port, connected=True, message=f"Порт {self.port}, {self.baud} бод")
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._ser is not None and self._loop is not None
        buf = b""
        try:
            while not self._stop.is_set():
                chunk = self._ser.read(256)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._loop.call_soon_threadsafe(self.on_raw, line + b"\n")
                if len(buf) > 4096:  # устройство шлёт без перевода строки — отдаём как есть
                    self._loop.call_soon_threadsafe(self.on_raw, buf)
                    buf = b""
        except (serial.SerialException, OSError) as exc:
            self._loop.call_soon_threadsafe(
                lambda: self.on_status(kind=self.kind, name=self.port, connected=False, message=f"Порт закрыт: {exc}")
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 1.0)
        if self._ser:
            await asyncio.to_thread(self._ser.close)
        self.on_status(kind=None, name=None, connected=False, message="Не подключено")
