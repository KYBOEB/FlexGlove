"""Запуск: python -m flexglove [--demo] [--port 8000] [--no-browser]"""
from __future__ import annotations

import argparse
import logging
import threading
import webbrowser

import uvicorn

from .app import DEFAULT_DB, create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="FlexGlove — ПК-приложение перчатки-интерфейса")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="путь к базе SQLite")
    parser.add_argument("--demo", action="store_true", help="сразу включить демо-режим (без железа)")
    parser.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    url = f"http://{args.host}:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"\n  FlexGlove запущен: {url}\n  Остановить: Ctrl+C\n")
    uvicorn.run(create_app(args.db, demo=args.demo), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
