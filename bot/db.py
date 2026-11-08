"""SQLite-хранилище: скрипты, серверы и ключи клиентов."""
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
    name         TEXT    NOT NULL,               -- название ключа ({name} в шагах)
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
CREATE TABLE IF NOT EXISTS scripts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    filename      TEXT,                          -- файл, загружаемый в ~/vpnbot
    content       BLOB,
    source_url    TEXT,
    install_steps TEXT NOT NULL DEFAULT '',      -- строки, которые «печатаются» в терминал
    check_cmd     TEXT NOT NULL DEFAULT '',      -- успешна → установка уже есть
    key_steps     TEXT NOT NULL DEFAULT '',
    result_type   TEXT NOT NULL DEFAULT 'file',  -- file | output
    result_path   TEXT NOT NULL DEFAULT '',      -- напр. /root/{name}.ovpn
    key_regex     TEXT NOT NULL DEFAULT '',
    delete_steps  TEXT NOT NULL DEFAULT '',
    ip_steps      TEXT NOT NULL DEFAULT '',      -- команды при смене IP сервера
    enabled       INTEGER NOT NULL DEFAULT 1,
    version       INTEGER NOT NULL DEFAULT 1,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dealers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    tg_id        INTEGER UNIQUE,                 -- заполняется, когда диллер откроет ссылку
    tg_username  TEXT,
    invite_token TEXT    UNIQUE,
    active       INTEGER NOT NULL DEFAULT 1,     -- 0 = заблокирован
    created_at   INTEGER NOT NULL,
    key_limit    INTEGER                         -- макс. активных ключей; NULL = без лимита
);
CREATE TABLE IF NOT EXISTS server_scripts (
    server_id INTEGER NOT NULL,
    script_id INTEGER NOT NULL,
    version   INTEGER NOT NULL,
    PRIMARY KEY (server_id, script_id)
);
"""

# dealer_id: кто создал ключ (NULL — главный админ)
CLIENT_COLUMNS = {"script_id": "INTEGER", "value": "BLOB", "filename": "TEXT", "dealer_id": "INTEGER"}
SCRIPT_FIELDS = {"name", "filename", "content", "source_url", "install_steps", "check_cmd", "key_steps",
                 "result_type", "result_path", "key_regex", "delete_steps", "ip_steps", "enabled"}
# изменение этих полей требует повторной установки на серверах
REINSTALL_FIELDS = {"filename", "content", "install_steps", "check_cmd"}

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
            have = {r[1] for r in self.conn.execute("PRAGMA table_info(clients)")}
            for col, typ in CLIENT_COLUMNS.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {typ}")
            if "key_limit" not in {r[1] for r in self.conn.execute("PRAGMA table_info(dealers)")}:
                self.conn.execute("ALTER TABLE dealers ADD COLUMN key_limit INTEGER")

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

    def set_server_host(self, sid: int, host: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE servers SET host=? WHERE id=?", (host, sid))

    def set_server_access(self, sid: int, host: str, host_key: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE servers SET host=?, host_key=? WHERE id=?", (host, host_key, sid))

    def rename_server(self, sid: int, name: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE servers SET name=? WHERE id=?", (name, sid))

    def reset_installed(self, sid: int) -> None:
        """Сервер заменён новым — скрипты нужно ставить заново."""
        with self.conn:
            self.conn.execute("DELETE FROM server_scripts WHERE server_id=?", (sid,))

    def delete_server(self, sid: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM clients WHERE server_id=?", (sid,))
            self.conn.execute("DELETE FROM server_scripts WHERE server_id=?", (sid,))
            self.conn.execute("DELETE FROM servers WHERE id=?", (sid,))

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

    def add_client(self, sid: int, script_id: int, name: str, value: bytes, filename: Optional[str],
                   days: int = 0, note: Optional[str] = None, dealer_id: Optional[int] = None) -> int:
        now = int(time.time())
        expires = now + days * DAY if days > 0 else None
        with self.conn:
            self.conn.execute(
                "DELETE FROM clients WHERE server_id=? AND name=? AND status='revoked'", (sid, name))
            cur = self.conn.execute(
                "INSERT INTO clients(server_id, script_id, name, value, filename, invite_token, note,"
                " created_at, expires_at, dealer_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (sid, script_id, name, value, filename, new_token(), note, now, expires, dealer_id))
        return cur.lastrowid

    def name_taken(self, sid: int, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM clients WHERE server_id=? AND name=?",
                                 (sid, name)).fetchone() is not None

    def move_client(self, cid: int, sid: int, name: str, value: bytes, filename: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET server_id=?, name=?, value=?, filename=? WHERE id=?",
                              (sid, name, value, filename, cid))

    def set_value(self, cid: int, value: bytes) -> None:
        with self.conn:
            self.conn.execute("UPDATE clients SET value=? WHERE id=?", (value, cid))

    def server_clients(self, sid: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clients WHERE server_id=?", (sid,)).fetchall()

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

    def list_all(self, dealer_id: Optional[int] = None) -> list[sqlite3.Row]:
        """Все ключи (dealer_id=None) или только ключи одного диллера."""
        if dealer_id is None:
            return self.conn.execute("SELECT * FROM clients ORDER BY status, server_id, created_at").fetchall()
        return self.conn.execute("SELECT * FROM clients WHERE dealer_id=? ORDER BY status, server_id, created_at",
                                 (dealer_id,)).fetchall()

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

    # ---------- скрипты ----------
    def add_script(self, **fields) -> int:
        fields = {k: v for k, v in fields.items() if k in SCRIPT_FIELDS}
        cols = ", ".join(fields) + ", created_at"
        marks = ", ".join("?" * len(fields)) + ", ?"
        with self.conn:
            cur = self.conn.execute(f"INSERT INTO scripts ({cols}) VALUES ({marks})",
                                    (*fields.values(), int(time.time())))
        return cur.lastrowid

    def script(self, script_id) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()

    def scripts(self, only_enabled: bool = False) -> list[sqlite3.Row]:
        where = "WHERE enabled=1" if only_enabled else ""
        return self.conn.execute(f"SELECT * FROM scripts {where} ORDER BY id").fetchall()

    def update_script(self, script_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in SCRIPT_FIELDS}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        if REINSTALL_FIELDS & fields.keys():
            sets += ", version=version+1"
        with self.conn:
            self.conn.execute(f"UPDATE scripts SET {sets} WHERE id=?", (*fields.values(), script_id))

    def bump_script(self, script_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE scripts SET version=version+1 WHERE id=?", (script_id,))

    def delete_script(self, script_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM server_scripts WHERE script_id=?", (script_id,))
            self.conn.execute("UPDATE clients SET script_id=NULL WHERE script_id=?", (script_id,))
            self.conn.execute("DELETE FROM scripts WHERE id=?", (script_id,))

    def installed_version(self, sid: int, script_id: int) -> Optional[int]:
        row = self.conn.execute("SELECT version FROM server_scripts WHERE server_id=? AND script_id=?",
                                (sid, script_id)).fetchone()
        return row[0] if row else None

    def set_installed(self, sid: int, script_id: int, version: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO server_scripts(server_id, script_id, version) VALUES (?,?,?)"
                " ON CONFLICT(server_id, script_id) DO UPDATE SET version=excluded.version",
                (sid, script_id, version))

    def installed_scripts(self, sid: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT sc.* FROM scripts sc JOIN server_scripts ss ON ss.script_id=sc.id WHERE ss.server_id=?",
            (sid,)).fetchall()

    def script_servers(self, script_id: int, version: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM server_scripts WHERE script_id=? AND version=?",
                                 (script_id, version)).fetchone()[0]

    # ---------- диллеры ----------
    def add_dealer(self, name: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO dealers(name, invite_token, created_at) VALUES (?,?,?)",
                                    (name, "dl_" + new_token(), int(time.time())))
        return cur.lastrowid

    def dealer(self, did) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dealers WHERE id=?", (did,)).fetchone() if did else None

    def dealer_by_tg(self, tg_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dealers WHERE tg_id=?", (tg_id,)).fetchone()

    def dealer_by_token(self, token: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dealers WHERE invite_token=?", (token,)).fetchone()

    def dealers(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM dealers ORDER BY id").fetchall()

    def bind_dealer(self, did: int, tg_id: int, username: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE dealers SET tg_id=NULL, tg_username=NULL WHERE tg_id=? AND id<>?", (tg_id, did))
            self.conn.execute("UPDATE dealers SET tg_id=?, tg_username=? WHERE id=?", (tg_id, username, did))

    def reset_dealer_invite(self, did: int) -> str:
        token = "dl_" + new_token()
        with self.conn:
            self.conn.execute("UPDATE dealers SET invite_token=?, tg_id=NULL, tg_username=NULL WHERE id=?",
                              (token, did))
        return token

    def set_dealer_active(self, did: int, active: bool) -> None:
        with self.conn:
            self.conn.execute("UPDATE dealers SET active=? WHERE id=?", (1 if active else 0, did))

    def delete_dealer(self, did: int) -> None:
        """Ключи диллера остаются и переходят главному админу."""
        with self.conn:
            self.conn.execute("UPDATE clients SET dealer_id=NULL WHERE dealer_id=?", (did,))
            self.conn.execute("DELETE FROM dealers WHERE id=?", (did,))

    def dealer_counts(self, did: int) -> tuple[int, int]:
        """(активных ключей, всего ключей)."""
        row = self.conn.execute(
            "SELECT SUM(status='active'), COUNT(*) FROM clients WHERE dealer_id=?", (did,)).fetchone()
        return row[0] or 0, row[1] or 0

    def set_dealer_limit(self, did: int, limit: Optional[int]) -> None:
        """limit=None — без лимита."""
        with self.conn:
            self.conn.execute("UPDATE dealers SET key_limit=? WHERE id=?", (limit, did))

    def dealer_slots_left(self, did: int) -> Optional[int]:
        """Сколько ещё активных ключей может создать диллер (None — без лимита)."""
        d = self.dealer(did)
        if d is None or d["key_limit"] is None:
            return None
        active, _ = self.dealer_counts(did)
        return max(0, d["key_limit"] - active)
