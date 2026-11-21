"""Одна очередь и один проигрыватель на Python-процесс; браузер только наблюдает.

SDL/pygame вызывается исключительно рабочим потоком. Ошибки устройства/декодера
публикуются отдельно от принятия задания. Для тестов подставляется driver без аудиоустройства.
"""
from __future__ import annotations

import importlib
import threading
from collections import deque
from pathlib import Path


class PygameDriver:
    def play(self, path: Path, cancelled: threading.Event) -> None:
        mixer = importlib.import_module("pygame.mixer")
        try:
            mixer.init()
            if cancelled.is_set():
                return
            mixer.music.load(str(path))
            if cancelled.is_set():
                return
            mixer.music.play(loops=0)
            while mixer.music.get_busy() and not cancelled.wait(0.02):
                pass
        finally:
            if mixer.get_init():
                mixer.music.stop()
                mixer.music.unload()
                mixer.quit()


class AudioPlayer:
    def __init__(self, folder: Path, driver=None, limit: int = 4):
        self.folder = folder.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.driver = driver or PygameDriver()
        self.limit = limit                    # всего заданий, включая текущее
        self.pending: deque[dict] = deque()
        self.active: dict | None = None
        self.results: deque[dict] = deque(maxlen=40)
        self.serial = 0
        self.condition = threading.Condition()
        self.cancelled = threading.Event()
        self.closed = False
        self.thread: threading.Thread | None = None

    def resolve(self, name: str | None) -> Path:
        if (not name or Path(name).name != name or "/" in name or "\\" in name or ":" in name
                or Path(name).suffix.lower() != ".mp3"):
            raise ValueError("Выберите MP3 из папки audio; произвольные пути запрещены")
        path = (self.folder / name).resolve()
        if path.parent != self.folder:
            raise ValueError("Файл вне папки audio")
        return path

    def available(self, name: str | None) -> bool:
        try:
            return self.resolve(name).is_file()
        except (ValueError, OSError):
            return False

    def files(self) -> list[str]:
        return sorted(p.name for p in self.folder.iterdir() if p.suffix.lower() == ".mp3" and self.available(p.name))

    def _result(self, job: dict, status: str, message: str = "") -> dict:
        result = {**job, "status": status, "message": message}
        self.results.appendleft(result)
        return result

    def enqueue(self, gesture_id: str, name: str | None, origin: str = "recognition") -> dict:
        with self.condition:
            self.serial += 1
            job = {"job_id": self.serial, "gesture_id": gesture_id, "file": name, "origin": origin}
            if not self.available(name):
                return self._result(job, "unavailable", "Аудио не назначено/недоступно")
            if self.closed:
                return self._result(job, "stopped", "Проигрыватель остановлен")
            if any(j["gesture_id"] == gesture_id for j in list(self.pending) + ([self.active] if self.active else [])):
                return self._result(job, "duplicate", "Этот жест уже ожидает или играет")
            if len(self.pending) + bool(self.active) >= self.limit:
                return self._result(job, "full", "Очередь заполнена")
            self.pending.append(job)
            result = self._result(job, "queued")
            self.condition.notify()
            return result

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._worker, name="flexglove-audio", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.condition:
            for job in self.pending:
                self._result(job, "cancelled")
            self.pending.clear()
            self.cancelled.set()
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.stop()
        if self.thread:
            self.thread.join(timeout=3)

    def process_next(self) -> bool:
        """Один шаг рабочего потока; тесты вызывают его синхронно с тестовым driver."""
        with self.condition:
            if not self.pending or self.active or self.closed:
                return False
            job = self.active = self.pending.popleft()
            self.cancelled = threading.Event()
            cancel = self.cancelled
            self._result(job, "playing")
        try:
            path = self.resolve(job["file"])
            if not path.is_file():
                raise FileNotFoundError("MP3 больше недоступен")
            self.driver.play(path, cancel)
        except Exception as exc:  # граница внешнего декодера/устройства; ошибка видна в API
            with self.condition:
                self._result(job, "cancelled" if cancel.is_set() else "error", str(exc))
        else:
            with self.condition:
                self._result(job, "cancelled" if cancel.is_set() else "finished")
        finally:
            with self.condition:
                self.active = None
        return True

    def _worker(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.closed or bool(self.pending))
                if self.closed:
                    return
            self.process_next()

    def snapshot(self) -> dict:
        with self.condition:
            return {"active": dict(self.active) if self.active else None,
                    "queue": list(self.pending), "results": list(self.results), "limit": self.limit}
