"""Хранение настроек и привязок жестов в SQLite (файл data/flexglove.db)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    combo      TEXT PRIMARY KEY,           -- комбинация пальцев, например '01'
    action_id  TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL                    -- JSON
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()

    # --- привязки ---
    def bindings(self) -> dict[str, str]:
        rows = self._db.execute("SELECT combo, action_id FROM bindings").fetchall()
        return {combo: action for combo, action in rows}

    def set_binding(self, combo: str, action_id: str | None) -> None:
        if action_id is None:
            self._db.execute("DELETE FROM bindings WHERE combo = ?", (combo,))
        else:
            # одно действие — одна комбинация: снимаем старую привязку этого действия
            self._db.execute("DELETE FROM bindings WHERE action_id = ? AND combo != ?", (action_id, combo))
            self._db.execute(
                "INSERT INTO bindings(combo, action_id) VALUES(?, ?) "
                "ON CONFLICT(combo) DO UPDATE SET action_id = excluded.action_id, created_at = datetime('now')",
                (combo, action_id),
            )
        self._db.commit()

    def clear_bindings(self) -> None:
        self._db.execute("DELETE FROM bindings")
        self._db.commit()

    # --- настройки ---
    def get(self, key: str, default: Any = None) -> Any:
        row = self._db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()
