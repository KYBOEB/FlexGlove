from __future__ import annotations

from typing import Any, Callable

RawCallback = Callable[..., None]  # on_raw(data, tag=None)
StatusCallback = Callable[..., Any]


class Source:
    """Базовый класс источника. Источник вызывает on_raw для каждого сообщения
    и on_status при изменении состояния подключения."""

    kind = "base"

    def __init__(self, on_raw: RawCallback, on_status: StatusCallback):
        self.on_raw = on_raw
        self.on_status = on_status

    async def start(self) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - интерфейс
        raise NotImplementedError
