"""Подключение к VPN-серверам по SSH.

Как бот получает доступ к серверу:
  1. Админ один раз вводит пароль root в боте.
  2. Бот заходит по паролю и добавляет свой SSH-ключ в ~/.ssh/authorized_keys.
  3. Пароль нигде не сохраняется — дальше бот ходит на сервер по ключу.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
from pathlib import Path
from typing import Optional

import asyncssh

from .steps import PREP, OnOutput, OnStep, SessionResult, run_session

log = logging.getLogger(__name__)

SSH_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "
MAX_FILE = 20 * 1024 * 1024


class RemoteError(RuntimeError):
    pass


class RemoteAuthError(RemoteError):
    """Сервер не пускает бота по его SSH-ключу (новый сервер или ключ удалили) — нужен пароль."""


class RemoteHostKeyError(RemoteError):
    """У сервера другой SSH-ключ — скорее всего, это другой (новый) сервер — нужен пароль."""


def conn_error(host: str, port: int, e: BaseException) -> RemoteError:
    """Понятный текст вместо «Нет связи с IP:» с пустой причиной."""
    if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
        return RemoteError(f"{host} не отвечает — проверьте IP и что сервер включён")
    if isinstance(e, ConnectionRefusedError):
        return RemoteError(f"{host}: порт {port} закрыт — SSH не принимает подключения")
    if isinstance(e, OSError) and getattr(e, "errno", None) in (101, 113):
        return RemoteError(f"{host} недоступен из сети — проверьте IP")
    return RemoteError(f"Нет связи с {host}: {str(e) or type(e).__name__}")


def _stdin(input) -> dict:
    # asyncssh не закрывает stdin при пустом input — команда, читающая stdin, зависла бы
    return {"input": input} if input else {"stdin": asyncssh.DEVNULL}


def _known_hosts(host: str, port: int, host_key: Optional[str]):
    if not host_key:
        return None
    spec = host if port == 22 else f"[{host}]:{port}"
    return asyncssh.import_known_hosts(f"{spec} {host_key}\n")


class Server:
    """Одно SSH-подключение к серверу (переиспользуется между командами)."""

    def __init__(self, host: str, port: int, user: str, host_key: Optional[str],
                 client_key: asyncssh.SSHKey):
        self.host, self.port, self.user = host, port, user
        self.host_key = host_key
        self.client_key = client_key
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._lock = asyncio.Lock()
        self.busy = asyncio.Lock()   # одна «тяжёлая» операция (установка/ключ) за раз

    async def connect(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is None or self._conn.is_closed():
                try:
                    self._conn = await asyncio.wait_for(asyncssh.connect(
                        self.host, port=self.port, username=self.user,
                        client_keys=[self.client_key], agent_path=None, password=None,
                        known_hosts=_known_hosts(self.host, self.port, self.host_key),
                        keepalive_interval=30,
                    ), 20)
                except asyncssh.HostKeyNotVerifiable as e:
                    raise RemoteHostKeyError(f"У сервера {self.host} другой SSH-ключ — похоже, это новый сервер") from e
                except asyncssh.PermissionDenied as e:
                    raise RemoteAuthError(f"Сервер {self.host} не пускает бота по ключу — похоже, это новый "
                                          "сервер или ключ бота удалён") from e
                except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                    raise conn_error(self.host, self.port, e) from e
            return self._conn

    async def _retry(self, fn):
        for attempt in (1, 2):
            try:
                return await fn(await self.connect())
            except asyncssh.TimeoutError as e:
                raise RemoteError(f"Команда на {self.host} не завершилась вовремя") from e
            except (OSError, asyncssh.Error) as e:
                await self.close()
                if attempt == 2:
                    raise conn_error(self.host, self.port, e) from e

    async def run(self, cmd: str, timeout: int = 120, input: str = "") -> tuple[int, str]:
        """Команда в папке ~/vpnbot. Возвращает (код, вывод)."""
        async def go(conn):
            r = await conn.run(SSH_PATH + PREP + cmd, timeout=timeout, stderr=asyncssh.STDOUT, **_stdin(input))
            return (r.exit_status if r.exit_status is not None else 255), str(r.stdout or "")
        return await self._retry(go)

    async def upload(self, name: str, content: bytes) -> None:
        """Кладёт файл в ~/vpnbot/ и делает его исполняемым."""
        path = '"$VPNB_DIR"/' + shlex.quote(name)

        async def go(conn):
            r = await conn.run(SSH_PATH + PREP + f"cat > {path} && chmod +x {path}",
                               input=content, encoding=None)
            if r.exit_status:
                raise RemoteError(f"Не удалось загрузить {name}: {(r.stderr or b'').decode(errors='replace')}")
        await self._retry(go)

    async def read_file(self, path: str) -> bytes:
        rel = path[2:] if path.startswith("~/") else path
        sh = shlex.quote(rel) if rel.startswith("/") else '"$HOME"/' + shlex.quote(rel)

        async def go(conn):
            r = await conn.run(SSH_PATH + f"cat -- {sh}", stdin=asyncssh.DEVNULL, encoding=None)
            if r.exit_status:
                raise RemoteError(f"Файл не найден на сервере: {path}")
            if len(r.stdout) > MAX_FILE:
                raise RemoteError("Файл ключа слишком большой")
            return r.stdout
        return await self._retry(go)

    async def session(self, lines: list[str], timeout: int, on_step: OnStep | None = None,
                      on_output: OnOutput | None = None) -> SessionResult:
        # без повтора: если связь оборвалась посреди установки, запускать её заново вслепую нельзя
        conn = await self.connect()
        try:
            return await run_session(conn, lines, timeout, on_step, on_output=on_output)
        except (OSError, asyncssh.Error) as e:
            await self.close()
            raise RemoteError(f"Связь с {self.host} прервалась: {e or type(e).__name__}") from e

    async def ping(self, timeout: Optional[float] = None) -> None:
        """Проверка связи. timeout — общий предел (для быстрых экранов), без него — с повтором."""
        async def check():
            code, out = await self.run("id -u", timeout=30)
            if out.strip() != "0":
                raise RemoteError(f"На {self.host} нет прав root")
        if timeout is None:
            return await check()
        try:
            await asyncio.wait_for(check(), timeout)
        except asyncio.TimeoutError as e:
            await self.close()
            raise conn_error(self.host, self.port, e) from e

    async def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def load_client_key(path: Path) -> asyncssh.SSHKey:
    """SSH-ключ бота. Создаётся при первом запуске."""
    if path.exists():
        return asyncssh.read_private_key(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="openvpn-seller-bot")
    key.write_private_key(str(path))
    os.chmod(path, 0o600)
    return key


def public_key_line(key: asyncssh.SSHKey) -> str:
    return key.export_public_key().decode().strip()


async def bootstrap_ssh(host: str, port: int, user: str, password: str, pubkey: str) -> str:
    """Заходит по паролю, добавляет ключ бота в authorized_keys. Возвращает ключ сервера."""
    try:
        conn = await asyncio.wait_for(asyncssh.connect(
            host, port=port, username=user, password=password,
            known_hosts=None, client_keys=None, agent_path=None,
        ), 25)
    except asyncssh.PermissionDenied:
        raise RemoteError("Неверный пароль — или на сервере запрещён вход root по паролю")
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        raise conn_error(host, port, e)
    async with conn:
        r = await conn.run("id -u", stdin=asyncssh.DEVNULL)
        if str(r.stdout).strip() != "0":
            raise RemoteError("Нужен пользователь root (у этого пользователя нет прав root).")
        q = shlex.quote(pubkey)
        r = await conn.run(
            "umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
            f"(grep -qxF {q} ~/.ssh/authorized_keys || echo {q} >> ~/.ssh/authorized_keys)",
            stdin=asyncssh.DEVNULL,
        )
        if r.exit_status:
            raise RemoteError(f"Не удалось добавить ключ: {r.stderr or r.stdout}")
        return conn.get_server_host_key().export_public_key().decode().strip()
