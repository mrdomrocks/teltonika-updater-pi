"""SQLite store for routers, backups, the event log, and settings."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


DEFAULTS = {
    "site_name": "Rutkeeper",
    "armed": "0",
    "when": "first_monday",
    "hour": "4",
    "minute": "0",
    "backup_kind": "device",
    "rms_token_enc": "",
    "rms_base": "https://api.rms.teltonika-networks.com",
    "rms_group_id": "",
    "rms_device_ids": "",
    "rms_task_ids": "",
    "rms_in_schedule": "0",
    "last_slot": "",
    "missed_note": "",
}


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                host TEXT NOT NULL,
                username TEXT NOT NULL DEFAULT 'admin',
                password_enc TEXT NOT NULL DEFAULT '',
                verify_tls INTEGER NOT NULL DEFAULT 0,
                product_code TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                serial TEXT NOT NULL DEFAULT '',
                firmware TEXT NOT NULL DEFAULT '',
                update_version TEXT NOT NULL DEFAULT '',
                update_state TEXT NOT NULL DEFAULT 'unknown',
                last_seen TEXT,
                last_backup_at TEXT,
                last_error TEXT NOT NULL DEFAULT '',
                last_detail TEXT NOT NULL DEFAULT '',
                auto_apply INTEGER NOT NULL DEFAULT 0,
                keep_settings INTEGER NOT NULL DEFAULT 1,
                schedule_enabled INTEGER NOT NULL DEFAULT 1,
                is_demo INTEGER NOT NULL DEFAULT 0,
                busy INTEGER NOT NULL DEFAULT 0,
                busy_action TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY,
                device_id INTEGER,
                filename TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                product_code TEXT NOT NULL DEFAULT '',
                firmware TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'device',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                is_profile INTEGER NOT NULL DEFAULT 0,
                profile_name TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                device_id INTEGER,
                level TEXT NOT NULL,
                action TEXT NOT NULL,
                message TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_backups_product ON backups(product_code);
            """
        )
        for key, value in DEFAULTS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def settings(self) -> dict[str, str]:
        with self._lock:
            rows = self.conn.execute("SELECT key, value FROM settings").fetchall()
        found = {row["key"]: row["value"] for row in rows}
        merged = dict(DEFAULTS)
        merged.update(found)
        return merged

    def set_settings(self, values: dict[str, str]) -> None:
        with self._lock:
            for key, value in values.items():
                self.conn.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )
            self.conn.commit()

    def seed_demos(self) -> None:
        with self._lock:
            count = self.conn.execute("SELECT COUNT(*) AS n FROM devices").fetchone()["n"]
            if count:
                return
            now = utcnow()
            rows = [
                (
                    "North gate",
                    "demo://rut241",
                    "admin",
                    1,
                    "RUT241-DEMO",
                    "RUT241",
                    "6000000001",
                    "RUT2_R_00.07.15.4",
                    "",
                    "current",
                    "Gate cabinet. Simulated router, already on the stable build.",
                ),
                (
                    "Yard pump",
                    "demo://rutx50",
                    "admin",
                    1,
                    "RUTX50-DEMO",
                    "RUTX50",
                    "6000000002",
                    "RUTX_R_00.07.17.3",
                    "RUTX_R_00.07.18.1",
                    "available",
                    "Pump shed. Simulated router with a stable package waiting.",
                ),
                (
                    "Cold store",
                    "demo://trb500.offline",
                    "admin",
                    1,
                    "TRB500-DEMO",
                    "TRB500",
                    "6000000003",
                    "TRB5_R_00.07.14.2",
                    "",
                    "offline",
                    "Cold store. Simulated router that does not answer.",
                ),
            ]
            for name, host, username, is_demo, code, model, serial, fw, update, state, notes in rows:
                self.conn.execute(
                    """
                    INSERT INTO devices (
                        name, host, username, password_enc, verify_tls, product_code, model,
                        serial, firmware, update_version, update_state, is_demo, notes, created_at
                    ) VALUES (?, ?, ?, '', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, host, username, code, model, serial, fw, update, state, is_demo, notes, now),
                )
            self.conn.commit()

    def devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM devices ORDER BY name COLLATE NOCASE").fetchall()
        return [dict(row) for row in rows]

    def device(self, device_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def insert_device(self, fields: dict[str, Any]) -> int:
        fields = dict(fields)
        fields.setdefault("created_at", utcnow())
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO devices ({columns}) VALUES ({marks})",
                tuple(fields.values()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_device(self, device_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE devices SET {assigns} WHERE id = ?",
                (*fields.values(), device_id),
            )
            self.conn.commit()

    def delete_device(self, device_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
            self.conn.commit()

    def delete_demos(self) -> int:
        with self._lock:
            cur = self.conn.execute("DELETE FROM devices WHERE is_demo = 1")
            self.conn.commit()
            return cur.rowcount

    def add_event(
        self,
        level: str,
        action: str,
        message: str,
        *,
        device_id: int | None = None,
        detail: str = "",
    ) -> None:
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO events (device_id, level, action, message, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (device_id, level, action, message, detail[:4000], utcnow()),
            )
            self.conn.commit()

    def events(self, limit: int = 200, device_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if device_id is None:
                rows = self.conn.execute(
                    """
                    SELECT e.*, d.name AS device_name
                    FROM events e
                    LEFT JOIN devices d ON d.id = e.device_id
                    ORDER BY e.id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    """
                    SELECT e.*, d.name AS device_name
                    FROM events e
                    LEFT JOIN devices d ON d.id = e.device_id
                    WHERE e.device_id = ?
                    ORDER BY e.id DESC
                    LIMIT ?
                    """,
                    (device_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def insert_backup(self, fields: dict[str, Any]) -> int:
        fields = dict(fields)
        fields.setdefault("created_at", utcnow())
        columns = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO backups ({columns}) VALUES ({marks})",
                tuple(fields.values()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def backups(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT b.*, d.name AS device_name
                FROM backups b
                LEFT JOIN devices d ON d.id = b.device_id
                ORDER BY b.id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def backup(self, backup_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT b.*, d.name AS device_name
                FROM backups b
                LEFT JOIN devices d ON d.id = b.device_id
                WHERE b.id = ?
                """,
                (backup_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_backup(self, backup_id: int, fields: dict[str, Any]) -> None:
        assigns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self.conn.execute(
                f"UPDATE backups SET {assigns} WHERE id = ?",
                (*fields.values(), backup_id),
            )
            self.conn.commit()

    def delete_backup(self, backup_id: int) -> dict[str, Any] | None:
        row = self.backup(backup_id)
        if not row:
            return None
        with self._lock:
            self.conn.execute("DELETE FROM backups WHERE id = ?", (backup_id,))
            self.conn.commit()
        return row

    def any_busy(self) -> bool:
        with self._lock:
            row = self.conn.execute("SELECT 1 FROM devices WHERE busy = 1 LIMIT 1").fetchone()
        return row is not None
