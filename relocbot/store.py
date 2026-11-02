"""Хранилище SQLite. Только стандартная библиотека."""
from __future__ import annotations

import json
import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    tz TEXT NOT NULL,
    premium_until TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    status_id TEXT NOT NULL,
    expiry TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id);
CREATE TABLE IF NOT EXISTS sent (
    profile_id INTEGER NOT NULL,
    lead INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    skipped INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_id, lead)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('in', 'out')),
    day TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id);
CREATE TABLE IF NOT EXISTS state (
    user_id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- пользователи ----------
    def ensure_user(self, uid: int, now_iso: str, default_tz: str) -> sqlite3.Row:
        self.db.execute(
            "INSERT OR IGNORE INTO users (id, tz, created_at) VALUES (?, ?, ?)",
            (uid, default_tz, now_iso),
        )
        self.db.commit()
        return self.get_user(uid)

    def get_user(self, uid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def set_tz(self, uid: int, tz: str) -> None:
        self.db.execute("UPDATE users SET tz = ? WHERE id = ?", (tz, uid))
        self.db.commit()

    def premium_until(self, uid: int) -> date | None:
        row = self.get_user(uid)
        if row is None or not row["premium_until"]:
            return None
        return date.fromisoformat(row["premium_until"])

    def set_premium_until(self, uid: int, until: date) -> None:
        self.db.execute("UPDATE users SET premium_until = ? WHERE id = ?", (until.isoformat(), uid))
        self.db.commit()

    def delete_user(self, uid: int) -> None:
        """Полное удаление всех данных пользователя."""
        pids = [r["id"] for r in self.db.execute("SELECT id FROM profiles WHERE user_id = ?", (uid,))]
        for pid in pids:
            self.db.execute("DELETE FROM sent WHERE profile_id = ?", (pid,))
        self.db.execute("DELETE FROM profiles WHERE user_id = ?", (uid,))
        self.db.execute("DELETE FROM events WHERE user_id = ?", (uid,))
        self.db.execute("DELETE FROM state WHERE user_id = ?", (uid,))
        self.db.execute("DELETE FROM users WHERE id = ?", (uid,))
        self.db.commit()

    # ---------- профили (сроки документов) ----------
    def add_profile(self, uid: int, status_id: str, expiry: date, now_iso: str) -> int:
        cur = self.db.execute(
            "INSERT INTO profiles (user_id, status_id, expiry, created_at) VALUES (?, ?, ?, ?)",
            (uid, status_id, expiry.isoformat(), now_iso),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def get_profile(self, pid: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()

    def list_profiles(self, uid: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM profiles WHERE user_id = ? ORDER BY expiry, id", (uid,)
        ).fetchall()

    def delete_profile(self, pid: int) -> None:
        self.db.execute("DELETE FROM sent WHERE profile_id = ?", (pid,))
        self.db.execute("DELETE FROM profiles WHERE id = ?", (pid,))
        self.db.commit()

    def profiles_with_tz(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT p.*, u.tz AS tz FROM profiles p JOIN users u ON u.id = p.user_id"
        ).fetchall()

    # ---------- отправленные напоминания ----------
    def sent_leads(self, pid: int) -> set[int]:
        rows = self.db.execute("SELECT lead FROM sent WHERE profile_id = ?", (pid,)).fetchall()
        return {r["lead"] for r in rows}

    def mark_sent(self, pid: int, leads, now_iso: str, skipped: bool) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO sent (profile_id, lead, sent_at, skipped) VALUES (?, ?, ?, ?)",
            [(pid, lead, now_iso, 1 if skipped else 0) for lead in leads],
        )
        self.db.commit()

    # ---------- въезды и выезды ----------
    def add_event(self, uid: int, kind: str, day: date, now_iso: str) -> None:
        self.db.execute(
            "INSERT INTO events (user_id, kind, day, created_at) VALUES (?, ?, ?, ?)",
            (uid, kind, day.isoformat(), now_iso),
        )
        self.db.commit()

    def events(self, uid: int) -> list[tuple[str, date]]:
        rows = self.db.execute(
            "SELECT kind, day FROM events WHERE user_id = ? ORDER BY day, id", (uid,)
        ).fetchall()
        return [(r["kind"], date.fromisoformat(r["day"])) for r in rows]

    def delete_last_event(self, uid: int) -> tuple[str, date] | None:
        row = self.db.execute(
            "SELECT id, kind, day FROM events WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)
        ).fetchone()
        if row is None:
            return None
        self.db.execute("DELETE FROM events WHERE id = ?", (row["id"],))
        self.db.commit()
        return row["kind"], date.fromisoformat(row["day"])

    # ---------- диалоговое состояние ----------
    def set_state(self, uid: int, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO state (user_id, payload) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET payload = excluded.payload",
            (uid, json.dumps(payload, ensure_ascii=False)),
        )
        self.db.commit()

    def get_state(self, uid: int) -> dict | None:
        row = self.db.execute("SELECT payload FROM state WHERE user_id = ?", (uid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def clear_state(self, uid: int) -> None:
        self.db.execute("DELETE FROM state WHERE user_id = ?", (uid,))
        self.db.commit()

    # ---------- служебное ----------
    def get_offset(self) -> int:
        row = self.db.execute("SELECT value FROM kv WHERE key = 'offset'").fetchone()
        return int(row["value"]) if row else 0

    def set_offset(self, offset: int) -> None:
        self.db.execute(
            "INSERT INTO kv (key, value) VALUES ('offset', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(offset),),
        )
        self.db.commit()

    def stats(self, today: date) -> dict[str, int]:
        one = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "profiles": one("SELECT COUNT(*) FROM profiles"),
            "premium": one(
                "SELECT COUNT(*) FROM users WHERE premium_until IS NOT NULL AND premium_until >= ?",
                today.isoformat(),
            ),
            "events": one("SELECT COUNT(*) FROM events"),
        }
