"""HTTP/WebSocket-сервер: REST для настроек и подключения, WebSocket для живого состояния."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .actions import ACTIONS, catalog
from .engine import FINGERS, Engine
from .sources import BleSource, MockSource, SerialSource, Source, list_serial_ports, scan_ble
from .storage import Storage

log = logging.getLogger("flexglove")
WEB_DIR = Path(__file__).parent / "web"
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "flexglove.db"


class ConnectRequest(BaseModel):
    kind: Literal["ble", "serial", "mock"]
    address: str | None = None
    name: str | None = None
    port: str | None = None
    baud: int = 115200


class BindingRequest(BaseModel):
    combo: str = Field(pattern=r"^[01]{1,8}$")
    action_id: str | None = None


class SettingsRequest(BaseModel):
    finger_map: list[str] | None = None
    debounce_ms: int | None = Field(default=None, ge=0, le=2000)


class MockRequest(BaseModel):
    bits: str | None = Field(default=None, pattern=r"^[01]{1,8}$")
    auto: bool | None = None


class AppState:
    def __init__(self, db_path: Path | str):
        self.storage = Storage(db_path)
        self.engine = Engine(self.storage)
        self.source: Source | None = None
        self.lock = asyncio.Lock()

    async def disconnect(self) -> None:
        if self.source is not None:
            try:
                await self.source.stop()
            finally:
                self.source = None

    async def connect(self, req: ConnectRequest) -> None:
        async with self.lock:
            await self.disconnect()
            on_raw, on_status = self.engine.on_raw, self.engine.on_source_status
            if req.kind == "mock":
                src: Source = MockSource(on_raw, on_status, n_channels=self.engine.n_channels or 2)
            elif req.kind == "serial":
                if not req.port:
                    raise HTTPException(400, "Не указан порт")
                src = SerialSource(on_raw, on_status, req.port, req.baud)
            else:
                if not req.address:
                    raise HTTPException(400, "Не указан адрес BLE-устройства")
                src = BleSource(on_raw, on_status, req.address, req.name)
            try:
                await src.start()
            except Exception as exc:  # noqa: BLE001 - показываем причину в интерфейсе
                log.exception("connect failed")
                try:
                    await src.stop()
                except Exception:  # noqa: BLE001
                    pass
                self.engine.on_source_status(kind=None, name=None, connected=False, message=f"Ошибка подключения: {exc}")
                raise HTTPException(502, f"Не удалось подключиться: {exc}") from exc
            self.source = src


def create_app(db_path: Path | str = DEFAULT_DB, demo: bool = False) -> FastAPI:
    state = AppState(db_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        state.engine.start()
        if demo:
            await state.connect(ConnectRequest(kind="mock"))
        yield
        await state.disconnect()
        await state.engine.stop()
        state.storage.close()

    app = FastAPI(title="FlexGlove host", lifespan=lifespan)
    app.state.flex = state

    # ---------- статика ----------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------- состояние и справочники ----------
    @app.get("/api/state")
    async def get_state():
        return state.engine.snapshot()

    @app.get("/api/meta")
    async def meta():
        return {"actions": catalog(), "fingers": FINGERS}

    # ---------- подключение ----------
    @app.get("/api/ble/scan")
    async def ble_scan(timeout: float = 5.0):
        try:
            return await scan_ble(timeout)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Поиск BLE не удался: {exc}. Проверьте, что Bluetooth включён и у Терминала/IDE есть доступ к Bluetooth.") from exc

    @app.get("/api/serial/ports")
    async def serial_ports():
        return list_serial_ports()

    @app.post("/api/connect")
    async def connect(req: ConnectRequest):
        await state.connect(req)
        return state.engine.snapshot()["source"]

    @app.post("/api/disconnect")
    async def disconnect():
        async with state.lock:
            await state.disconnect()
        return state.engine.snapshot()["source"]

    @app.post("/api/mock")
    async def mock(req: MockRequest):
        if not isinstance(state.source, MockSource):
            raise HTTPException(409, "Демо-режим не включён")
        if req.bits is not None:
            state.source.set_bits(req.bits)
        if req.auto is not None:
            state.source.set_auto(req.auto)
        return {"bits": state.source.bits, "auto": state.source.auto}

    # ---------- привязки и настройки ----------
    @app.post("/api/bindings")
    async def set_binding(req: BindingRequest):
        if req.action_id is not None and req.action_id not in ACTIONS:
            raise HTTPException(400, "Неизвестное действие")
        if req.action_id is not None and set(req.combo) == {"0"}:
            raise HTTPException(400, "Комбинация «все пальцы разогнуты» — состояние покоя, её нельзя привязать")
        state.engine.set_binding(req.combo, req.action_id)
        return state.storage.bindings()

    @app.delete("/api/bindings")
    async def clear_bindings():
        state.storage.clear_bindings()
        state.engine._dirty = True  # noqa: SLF001
        return {}

    @app.post("/api/settings")
    async def settings(req: SettingsRequest):
        if req.finger_map is not None and any(f not in FINGERS for f in req.finger_map):
            raise HTTPException(400, "Неизвестный палец")
        state.engine.set_settings(req.finger_map, req.debounce_ms)
        return {"finger_map": state.engine.finger_map, "debounce_ms": state.engine.debounce_ms}

    @app.post("/api/actions/{action_id}/toggle")
    async def toggle(action_id: str):
        if action_id not in ACTIONS:
            raise HTTPException(404, "Неизвестное действие")
        state.engine.toggle_action(action_id, source="manual")
        return state.engine.action_state

    @app.post("/api/actions/reset")
    async def reset():
        state.engine.reset_actions()
        return state.engine.action_state

    # ---------- живое состояние ----------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = state.engine.subscribe()
        await websocket.send_json(state.engine.snapshot())
        try:
            while True:
                await websocket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            state.engine.unsubscribe(q)

    return app
