"""Запуск как обычного приложения: без консоли, интерфейс — в браузере по умолчанию,
значок в области уведомлений (трее) Windows с меню «Открыть / Записи / Журнал / Выход».

    pythonw -m flexglove.desktop [--demo] [--port 8000] [--no-tray]

Если приложение уже запущено — просто открывает его вкладку в браузере (второй копии не будет).
Но если запущена ДРУГАЯ версия или копия из другой папки (например, старая v0.5.0), её просят остановиться
(/api/shutdown) и запускают эту — иначе ярлык показывал бы старую версию. Старые версии без /api/shutdown
продолжают работать, а новая запускается на следующем свободном порту.
Журнал работы — data/flexglove.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .__main__ import free_port
from .app import DATA_DIR, DEFAULT_DB, PROJECT_ROOT, WEB_DIR, create_app

LOG_FILE = DATA_DIR / "flexglove.log"
log = logging.getLogger("flexglove.desktop")


def setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stdout is None or sys.stderr is None:  # pythonw.exe: консоли нет — всё в журнал
        stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def ping(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url + "/api/ping", timeout=0.5) as r:
            data = json.loads(r.read())
            return data if data.get("app") == "flexglove" else None
    except (OSError, ValueError):
        return None


def is_flexglove(url: str) -> bool:
    return ping(url) is not None


def same_instance(info: dict) -> bool:
    """Запущена ли та же версия из той же папки, что и эта копия."""
    return info.get("version") == __version__ and Path(info.get("root", "")) == PROJECT_ROOT


def stop_other(url: str, timeout: float = 5.0) -> bool:
    """Попросить другую копию остановиться. True — остановилась."""
    try:
        req = urllib.request.Request(url + "/api/shutdown", method="POST", data=b"")
        urllib.request.urlopen(req, timeout=1.0).close()
    except OSError:
        return False                                   # старая версия без /api/shutdown
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_flexglove(url):
            return True
        time.sleep(0.2)
    return False


def wait_ready(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_flexglove(url):
            return True
        time.sleep(0.2)
    return False


def open_path(path: Path) -> None:
    if path.suffix == "":
        path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def run_tray(url: str, server: uvicorn.Server, holder: dict | None = None) -> None:
    import pystray
    from PIL import Image

    def stop(icon, _item=None):
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть FlexGlove", lambda *_: webbrowser.open(url), default=True),
        pystray.MenuItem("Папка с записями", lambda *_: open_path(DATA_DIR / "recordings")),
        pystray.MenuItem("Журнал программы", lambda *_: open_path(LOG_FILE)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", stop),
    )
    icon = pystray.Icon("FlexGlove", Image.open(WEB_DIR / "icon.png"), f"FlexGlove {__version__} — {url}", menu)
    if holder is not None:
        holder["icon"] = icon
    icon.run()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="FlexGlove — запуск как приложения")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-tray", action="store_true", help="без значка в трее (остановка — Ctrl+C)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    setup_logging()

    # уже запущено? та же версия из этой папки — открываем вкладку и выходим; другая — останавливаем её
    for p in range(args.port, args.port + 10):
        url = f"http://{args.host}:{p}"
        info = ping(url)
        if info is None:
            continue
        if same_instance(info):
            log.info("already running at %s", url)
            if not args.no_browser:
                webbrowser.open(url)
            return
        log.info("another FlexGlove at %s (version %s, %s) — asking it to stop", url, info.get("version"), info.get("root"))
        if not stop_other(url):
            log.warning("could not stop FlexGlove at %s — starting this version on another port", url)

    port = free_port(args.host, args.port)
    url = f"http://{args.host}:{port}"
    app = create_app(DEFAULT_DB, demo=args.demo)
    config = uvicorn.Config(app, host=args.host, port=port, log_config=None, log_level="warning")
    server = uvicorn.Server(config)
    tray: dict = {}

    def shutdown() -> None:                            # /api/shutdown — новая копия просит уступить место
        server.should_exit = True
        if tray.get("icon") is not None:
            tray["icon"].stop()
    app.state.shutdown = shutdown
    thread = threading.Thread(target=server.run, name="server", daemon=True)
    thread.start()
    log.info("FlexGlove %s starting at %s", __version__, url)

    if not wait_ready(url):
        log.error("server did not start, see log above")
        server.should_exit = True
        sys.exit(1)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        if args.no_tray:
            raise ImportError("tray disabled")
        run_tray(url, server, tray)
    except Exception as exc:  # noqa: BLE001 - нет pystray / нет трея в системе — работаем без значка
        log.info("tray not used: %s", exc)
        try:
            while thread.is_alive():
                thread.join(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        server.should_exit = True
        thread.join(5)
        log.info("FlexGlove stopped")


if __name__ == "__main__":
    main()
