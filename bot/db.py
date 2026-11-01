"""SQLite-хранилище клиентов бота."""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL UNIQUE,      -- имя сертификата (CN) в easy-rsa
    tg_id        INTEGER,                      -- Telegram ID владельца ключа
    tg_username  TEXT,
    invite_token TEXT    UNIQUE,               -- токен из ссылки t.me/bot?start=...
    note         TEXT,                         -- заметка админа (имя клиента, телефон...)
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,                      -- NULL = бессрочно
    status       TEXT    NOT NULL DEFAULT 'active',   -- active | revoked
    warned       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_clients_tg ON clients(tg_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

DAY = 86400


def new_token() -> str:
    # Telegram deep-link допускает A-Z a-z 0-9 _ - (до 64 символов)
    return secrets.token_urlsafe(16)


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.executescript(SCHEMA)

    # ---------- клиенты ----------
    def get(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE name=?", (name,)).fetchone()

    def get_by_token(self, token: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE invite_token=?", (token,)
        ).fetchone()

    def add_client(self, name: str, days: int = 0, note: Optional[str] = None) -> str:
        """Создаёт запись клиента и возвращает invite-токен. days=0 — бессрочно."""
        now = int(time.time())
        expires = now + days * DAY if days > 0 else None
        token = new_token()
        with self.conn:
            # освободить имя, если раньше был такой же, но отключённый ключ
            self.conn.execute("DELETE FROM clients WHERE name=? AND status='revoked'", (name,))
            self.conn.execute(
                "INSERT INTO clients(name, invite_token, note, created_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (name, token, note, now, expires),
            )
        return token

    def bind(self, name: str, tg_id: int, username: Optional[str]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE clients SET tg_id=?, tg_username=? WHERE name=?",
                (tg_id, username, name),
            )

    def reset_invite(self, name: str) -> str:
        """Новая ссылка-приглашение; старая перестаёт работать, привязка снимается."""
        token = new_token()
        with self.conn:
            self.conn.execute(
                "UPDATE clients SET invite_token=?, tg_id=NULL, tg_username=NULL WHERE name=?",
                (token, name),
            )
        return token

    def set_status(self, name: str, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET status=? WHERE name=?", (status, name))

    def extend(self, name: str, days: int) -> Optional[int]:
        """Продлить на days дней (0 — сделать бессрочным). Возвращает новый expires_at."""
        row = self.get(name)
        if row is None:
            return None
        if days <= 0:
            new_exp = None
        else:
            base = max(int(time.time()), row["expires_at"] or 0)
            new_exp = base + days * DAY
        with self.conn:
            self.conn.execute(
                "UPDATE clients SET expires_at=?, warned=0 WHERE name=?", (new_exp, name)
            )
        return new_exp

    def mark_warned(self, name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET warned=1 WHERE name=?", (name,))

    def list_all(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients ORDER BY status, created_at"
        ).fetchall()

    def active(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE status='active'").fetchall()

    def active_bound(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND tg_id IS NOT NULL"
        ).fetchall()

    def clients_of(self, tg_id: int, only_active: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM clients WHERE tg_id=?"
        if only_active:
            sql += " AND status='active'"
        return self.conn.execute(sql + " ORDER BY created_at", (tg_id,)).fetchall()

    def user_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT tg_id FROM clients WHERE status='active' AND tg_id IS NOT NULL"
        ).fetchall()
        return [r[0] for r in rows]

    def expired(self, now: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND expires_at IS NOT NULL"
            " AND expires_at<=?",
            (now,),
        ).fetchall()

    def expiring(self, until: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND warned=0"
            " AND expires_at IS NOT NULL AND expires_at<=?",
            (until,),
        ).fetchall()

    # ---------- настройки ----------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
