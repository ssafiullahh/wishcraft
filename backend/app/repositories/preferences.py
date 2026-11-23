"""SQLite repository for remembered defaults (concept 5: AI memory)."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class PreferencesRepository:
    """Persist one local user's preferences without adding a database server."""

    COLUMNS = ("language", "tone", "country", "region", "timezone")
    DEFAULTS = {
        "language": "English",
        "tone": "Warm and heartfelt",
        "country": "Canada",
        "region": "Ontario",
        "timezone": "America/Toronto",
    }

    def __init__(self, db_path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[2] / "data" / "wishcraft.sqlite3"
        configured_path = Path(db_path or os.getenv("WISHCRAFT_DB_PATH", default_path))
        self.db_path = configured_path if configured_path.is_absolute() else Path(__file__).resolve().parents[3] / configured_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS preferences (
                        id INTEGER PRIMARY KEY CHECK (id = 1),
                        language TEXT NOT NULL,
                        tone TEXT NOT NULL,
                        country TEXT NOT NULL,
                        region TEXT NOT NULL,
                        timezone TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                columns = ", ".join(self.COLUMNS)
                placeholders = ", ".join("?" for _ in self.COLUMNS)
                connection.execute(
                    f"INSERT OR IGNORE INTO preferences (id, {columns}) VALUES (1, {placeholders})",
                    tuple(self.DEFAULTS[name] for name in self.COLUMNS),
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def get(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                f"SELECT {', '.join(self.COLUMNS)} FROM preferences WHERE id = 1"
            ).fetchone()
        return dict(row) if row else dict(self.DEFAULTS)

    def save(self, values: dict[str, Any]) -> dict[str, Any]:
        fields = {name: str(values[name]).strip() for name in self.COLUMNS}
        assignments = ", ".join(f"{name} = ?" for name in self.COLUMNS)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute(
                    f"UPDATE preferences SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                    tuple(fields[name] for name in self.COLUMNS),
                )
        return fields
