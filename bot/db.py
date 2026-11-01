"""SQLite-хранилище: серверы и ключи клиентов."""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'ssh',   -- ssh | local (бот стоит на этом сервере)
    host       TEXT    NOT NULL,                 -- IP или домен для SSH
    port       INTEGER NOT NULL DEFAULT 22,
    user       TEXT    NOT NULL DEFAULT 'root',
    host_key   TEXT,                             -- ключ SSH-сервера (защита от подмены)
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id    INTEGER,
    name         TEXT    NOT NULL,               -- имя сертификата (CN) в easy-rsa
    tg_id        INTEGER,                        -- Telegram ID владельца ключа
    tg_username  TEXT,
    invite_token TEXT    UNIQUE,                 -- токен из ссылки t.me/bot?start=...
    note         TEXT,
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,                        -- NULL = бессрочно
    status       TEXT    NOT NULL DEFAULT 'active',   -- active | revoked
    warned       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(server_id, name)
);
CREATE INDEX IF NOT EXISTS idx_clients_tg ON clients(tg_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

DAY = 86400


def new_token() -> str:
    return secrets.token_urlsafe(16)


class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._migrate()
        with self.conn:
            self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Старая версия базы (без серверов) → новая."""
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(clients)")]
        if cols and "server_id" not in cols:
            with self.conn:
                self.conn.execute("ALTER TABLE clients RENAME TO clients_old")
                self.conn.executescript(SCHEMA)
                self.conn.execute(
                    "INSERT INTO clients(name, tg_id, tg_username, invite_token, note, created_at,"
                    " expires_at, status, warned) SELECT name, tg_id, tg_username, invite_token,"
                    " note, created_at, expires_at, status, warned FROM clients_old")
                self.conn.execute("DROP TABLE clients_old")

    # ---------- серверы ----------
    def add_server(self, name: str, kind: str, host: str, port: int = 22, user: str = "root",
                   host_key: Optional[str] = None) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO servers(name, kind, host, port, user, host_key, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (name, kind, host, port, user, host_key, int(time.time())))
        return cur.lastrowid

    def server(self, sid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM servers WHERE id=?", (sid,)).fetchone()

    def servers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM servers ORDER BY id").fetchall()

    def local_server(self) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM servers WHERE kind='local'").fetchone()

    def set_server_host(self, sid: int, host: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE servers SET host=? WHERE id=?", (host, sid))

    def delete_server(self, sid: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM clients WHERE server_id=?", (sid,))
            self.conn.execute("DELETE FROM servers WHERE id=?", (sid,))

    def assign_orphans(self, sid: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET server_id=? WHERE server_id IS NULL", (sid,))

    def count_active(self, sid: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM clients WHERE server_id=? AND status='active'", (sid,)).fetchone()[0]

    # ---------- ключи ----------
    def get(self, cid: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()

    def get_by_name(self, sid: int, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE server_id=? AND name=?", (sid, name)).fetchone()

    def get_by_token(self, token: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE invite_token=?", (token,)).fetchone()

    def add_client(self, sid: int, name: str, days: int = 0, note: Optional[str] = None) -> int:
        now = int(time.time())
        expires = now + days * DAY if days > 0 else None
        with self.conn:
            self.conn.execute(
                "DELETE FROM clients WHERE server_id=? AND name=? AND status='revoked'", (sid, name))
            cur = self.conn.execute(
                "INSERT INTO clients(server_id, name, invite_token, note, created_at, expires_at)"
                " VALUES (?,?,?,?,?,?)", (sid, name, new_token(), note, now, expires))
        return cur.lastrowid

    def move_client(self, cid: int, sid: int, name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET server_id=?, name=? WHERE id=?", (sid, name, cid))

    def bind(self, cid: int, tg_id: int, username: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET tg_id=?, tg_username=? WHERE id=?",
                              (tg_id, username, cid))

    def reset_invite(self, cid: int) -> str:
        token = new_token()
        with self.conn:
            self.conn.execute(
                "UPDATE clients SET invite_token=?, tg_id=NULL, tg_username=NULL WHERE id=?",
                (token, cid))
        return token

    def set_status(self, cid: int, status: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET status=? WHERE id=?", (status, cid))

    def extend(self, cid: int, days: int) -> Optional[int]:
        row = self.get(cid)
        if row is None:
            return None
        new_exp = None if days <= 0 else max(int(time.time()), row["expires_at"] or 0) + days * DAY
        with self.conn:
            self.conn.execute("UPDATE clients SET expires_at=?, warned=0 WHERE id=?", (new_exp, cid))
        return new_exp

    def mark_warned(self, cid: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET warned=1 WHERE id=?", (cid,))

    def list_all(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients ORDER BY status, server_id, created_at").fetchall()

    def active(self, sid: Optional[int] = None) -> list[sqlite3.Row]:
        if sid is None:
            return self.conn.execute("SELECT * FROM clients WHERE status='active'").fetchall()
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND server_id=?", (sid,)).fetchall()

    def active_bound(self, sid: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND tg_id IS NOT NULL AND server_id=?",
            (sid,)).fetchall()

    def clients_of(self, tg_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE tg_id=? AND status='active' ORDER BY created_at",
            (tg_id,)).fetchall()

    def user_ids(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT tg_id FROM clients WHERE status='active' AND tg_id IS NOT NULL").fetchall()
        return [r[0] for r in rows]

    def expired(self, now: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND expires_at IS NOT NULL AND expires_at<=?",
            (now,)).fetchall()

    def expiring(self, until: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clients WHERE status='active' AND warned=0"
            " AND expires_at IS NOT NULL AND expires_at<=?", (until,)).fetchall()
