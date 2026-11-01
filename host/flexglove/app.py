"""HTTP/WebSocket-сервер: REST для настроек и подключения, WebSocket для живого состояния."""
from __future__ import annotations

import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .actions import ACTIONS, catalog
from .dsp import presets as dsp_presets
from .dsp.filters import FILTERS
from .imu import mount_catalog
from .engine import FINGERS, Engine
from .sources import BleSource, MockSource, ReplaySource, SerialSource, Source, list_serial_ports, scan_ble
from .storage import Storage

log = logging.getLogger("flexglove")
# На Windows реестр иногда сопоставляет .js с text/plain — тогда браузер откажется загружать ES-модуль
# (hand3d.js, three.module.min.js). Задаём тип явно.
mimetypes.add_type("text/javascript", ".js")
WEB_DIR = Path(__file__).parent / "web"
DATA_DIR = Path(__file__).resolve().parents[2] / "data"
DEFAULT_DB = DATA_DIR / "flexglove.db"


class ConnectRequest(BaseModel):
    kind: Literal["ble", "serial", "mock", "replay"]
    address: str | None = None
    name: str | None = None
    port: str | None = None
    baud: int = 115200
    text: str | None = Field(default=None, max_length=50_000_000)   # содержимое CSV для replay
    recording: str | None = None                                     # или имя файла из data/recordings
    loop: bool = True
    n_channels: int = Field(default=5, ge=1, le=8)                   # число сенсоров в демо-режиме


class BindingRequest(BaseModel):
    combo: str = Field(pattern=r"^[01]{1,8}$")
    action_id: str | None = None


class RoleModel(BaseModel):
    grip: bool = True      # канал задаёт сжатие захвата
    cmd: bool = True       # канал участвует в жестах-командах


class SettingsRequest(BaseModel):
    finger_map: list[str] | None = None
    debounce_ms: int | None = Field(default=None, ge=0, le=2000)
    channel_roles: list[RoleModel] | None = Field(default=None, max_length=8)
    imu_mount: str | None = None
    hand: Literal["right", "left"] | None = None


class PipelineRequest(BaseModel):
    preset: str                                  # basic | one_euro | smooth | custom
    config: list[dict] | None = Field(default=None, max_length=16)   # для custom


class MockRequest(BaseModel):
    bits: str | None = Field(default=None, pattern=r"^[01]{1,8}$")
    bends: list[float] | None = None
    auto: bool | None = None
    roll: float | None = Field(default=None, ge=-180, le=180)     # имитация IMU: целевые углы кисти, °
    pitch: float | None = Field(default=None, ge=-180, le=180)
    yaw: float | None = Field(default=None, ge=-180, le=180)


class AppState:
    def __init__(self, db_path: Path | str):
        self.storage = Storage(db_path)
        data_dir = Path(db_path).parent if str(db_path) != ":memory:" else DATA_DIR
        self.engine = Engine(self.storage, data_dir)
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
                src: Source = MockSource(on_raw, on_status, n_channels=req.n_channels)
            elif req.kind == "serial":
                if not req.port:
                    raise HTTPException(400, "Не указан порт")
                src = SerialSource(on_raw, on_status, req.port, req.baud)
            elif req.kind == "replay":
                text, name = req.text, req.name or "запись"
                if req.recording:
                    path = self.engine.recorder.folder / Path(req.recording).name
                    if not path.exists():
                        raise HTTPException(404, "Запись не найдена")
                    text, name = path.read_text(encoding="utf-8-sig"), path.name
                if not text:
                    raise HTTPException(400, "Нет данных для воспроизведения")
                try:
                    src = ReplaySource(on_raw, on_status, text, name=name, loop=req.loop)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
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
    engine = state.engine

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        engine.start()
        if demo:
            await state.connect(ConnectRequest(kind="mock"))
        yield
        await state.disconnect()
        await engine.stop()
        state.storage.close()

    app = FastAPI(title="FlexGlove host", lifespan=lifespan)
    app.state.flex = state

    # ---------- статика ----------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------- служебное ----------
    @app.get("/api/ping")
    async def ping():
        return {"app": "flexglove", "version": __version__}

    @app.get("/api/state")
    async def get_state():
        return engine.snapshot()

    @app.get("/api/meta")
    async def meta():
        return {"actions": catalog(), "fingers": FINGERS, "version": __version__, "imu_mounts": mount_catalog()}

    # ---------- подключение ----------
    @app.get("/api/ble/scan")
    async def ble_scan(timeout: float = 5.0):
        try:
            return await scan_ble(timeout)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Поиск BLE не удался: {exc}. Проверьте, что Bluetooth включён.") from exc

    @app.get("/api/serial/ports")
    async def serial_ports():
        return list_serial_ports()

    @app.post("/api/connect")
    async def connect(req: ConnectRequest):
        await state.connect(req)
        return engine.snapshot()["source"]

    @app.post("/api/disconnect")
    async def disconnect():
        async with state.lock:
            await state.disconnect()
        return engine.snapshot()["source"]

    @app.post("/api/mock")
    async def mock(req: MockRequest):
        if not isinstance(state.source, MockSource):
            raise HTTPException(409, "Демо-режим не включён")
        if req.bits is not None:
            state.source.set_bits(req.bits)
        if req.bends is not None:
            state.source.set_bends(req.bends)
        if req.auto is not None:
            state.source.set_auto(req.auto)
        if any(v is not None for v in (req.roll, req.pitch, req.yaw)):
            state.source.set_angles(req.roll, req.pitch, req.yaw)
        return {"bits": state.source.bits, "auto": state.source.auto, "angles": state.source.angle_target}

    # ---------- калибровка ----------
    @app.post("/api/calibration/{target}")
    async def calibrate(target: Literal["open", "fist"]):
        try:
            engine.start_calibration(target)
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": target}

    @app.delete("/api/calibration")
    async def reset_calibration():
        engine.reset_calibration()
        return {}

    # ---------- запись ----------
    @app.post("/api/recording/start")
    async def rec_start():
        return {"file": engine.start_recording()}

    @app.post("/api/recording/stop")
    async def rec_stop():
        return {"file": engine.stop_recording()}

    @app.get("/api/recordings")
    async def recordings():
        return engine.recorder.list()

    @app.get("/api/recordings/{name}")
    async def recording_file(name: str):
        path = engine.recorder.folder / Path(name).name
        if not path.exists():
            raise HTTPException(404, "Запись не найдена")
        return FileResponse(path, filename=path.name, media_type="text/csv")

    # ---------- привязки и настройки ----------
    @app.post("/api/bindings")
    async def set_binding(req: BindingRequest):
        if req.action_id is not None and req.action_id not in ACTIONS:
            raise HTTPException(400, "Неизвестное действие")
        if req.action_id is not None and set(req.combo) == {"0"}:
            raise HTTPException(400, "Комбинация «все пальцы разогнуты» — состояние покоя, её нельзя привязать")
        engine.set_binding(req.combo, req.action_id)
        return state.storage.bindings()

    @app.delete("/api/bindings")
    async def clear_bindings():
        state.storage.clear_bindings()
        engine.set_settings()
        return {}

    @app.post("/api/settings")
    async def settings(req: SettingsRequest):
        if req.finger_map is not None and any(f not in FINGERS for f in req.finger_map):
            raise HTTPException(400, "Неизвестный палец")
        roles = [r.model_dump() for r in req.channel_roles] if req.channel_roles is not None else None
        try:
            engine.set_settings(req.finger_map, req.debounce_ms, roles, req.imu_mount, req.hand)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"finger_map": engine.finger_map, "debounce_ms": engine.debounce_ms,
                "channel_roles": [r.as_dict() for r in engine.roles()],
                "imu_mount": engine.imu_tracker.mount, "hand": engine.hand}

    # ---------- ориентация (IMU) ----------
    @app.post("/api/imu/zero")
    async def imu_zero():
        try:
            engine.imu_zero()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.snapshot()["orientation"]

    @app.post("/api/imu/calibrate")
    async def imu_calibrate():
        try:
            engine.imu_calibrate()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.snapshot()["orientation"]

    # ---------- фильтрация (dsp) ----------
    @app.get("/api/pipelines")
    async def pipelines():
        return {"presets": dsp_presets.catalog(), "filters": sorted(FILTERS),
                "current": engine.snapshot()["pipeline"]}

    @app.post("/api/pipeline")
    async def set_pipeline(req: PipelineRequest):
        try:
            engine.set_pipeline(req.preset, req.config)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return engine.snapshot()["pipeline"]

    @app.post("/api/actions/{action_id}/toggle")
    async def toggle(action_id: str):
        if action_id not in ACTIONS:
            raise HTTPException(404, "Неизвестное действие")
        engine.toggle_action(action_id, source="manual")
        return engine.action_state()

    @app.post("/api/actions/reset")
    async def reset():
        engine.reset_actions()
        return engine.action_state()

    # ---------- живое состояние ----------
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        q = engine.subscribe()
        await websocket.send_json(engine.snapshot())
        try:
            while True:
                await websocket.send_json(await q.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.unsubscribe(q)

    return app
