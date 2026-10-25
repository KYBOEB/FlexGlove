"""Запуск: python -m flexglove [--demo] [--port 8000] [--ui app|browser|none]

Консольный запуск для разработки (видны сообщения сервера). Обычный запуск без консоли —
FlexGlove.pyw или ярлык на рабочем столе (модуль flexglove.desktop).
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .app import DEFAULT_DB, create_app

WINDOWS_BROWSERS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


def _wait_server(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url + "/api/state", timeout=1).close()
            return
        except OSError:
            time.sleep(0.2)


def free_port(host: str, port: int, attempts: int = 10) -> int:
    """Возвращает port, если он свободен, иначе следующий свободный (порт 8000 бывает занят)."""
    import socket

    for p in range(port, port + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return port


def open_ui(url: str, mode: str) -> None:
    """Открывает интерфейс, когда сервер уже отвечает."""
    _wait_server(url)
    if mode == "app" and sys.platform == "win32":
        for raw in WINDOWS_BROWSERS:
            exe = os.path.expandvars(raw)
            if os.path.exists(exe):
                subprocess.Popen([exe, f"--app={url}", "--window-size=1400,900", "--new-window"])
                return
    webbrowser.open(url)


def main() -> None:
    for stream in (sys.stdout, sys.stderr):  # консоль Windows может не уметь в кириллицу
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="FlexGlove — ПК-приложение перчатки-интерфейса")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="путь к базе SQLite")
    parser.add_argument("--demo", action="store_true", help="сразу включить демо-режим (без железа)")
    parser.add_argument("--ui", choices=["browser", "app", "none"], default="browser",
                        help="browser — браузер по умолчанию, app — отдельное окно Edge/Chrome, none — не открывать")
    parser.add_argument("--no-browser", action="store_true", help="то же, что --ui none")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    ui = "none" if args.no_browser else args.ui
    if ui != "none":
        threading.Thread(target=open_ui, args=(url, ui), daemon=True).start()
    print(f"\n  FlexGlove запущен: {url}\n  Это окно не закрывайте, пока работаете. Остановить: Ctrl+C или закрыть окно.\n")
    uvicorn.run(create_app(args.db, demo=args.demo), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
