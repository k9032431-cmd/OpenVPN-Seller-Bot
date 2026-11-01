"""Выполнение команд на VPN-серверах: локально (бот стоит на этом же сервере) или по SSH.

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

log = logging.getLogger(__name__)

SSH_PATH = "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; "


class RemoteError(RuntimeError):
    pass


class Executor:
    async def run(self, cmd: str, input: str = "", timeout: int = 120) -> tuple[int, str]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class LocalExecutor(Executor):
    async def run(self, cmd: str, input: str = "", timeout: int = 120) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(input.encode()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RemoteError(f"Команда не завершилась за {timeout} с")
        return proc.returncode, out.decode(errors="replace")


def _stdin(input: str) -> dict:
    # asyncssh не закрывает stdin при пустом input — команда, читающая stdin, зависла бы
    return {"input": input} if input else {"stdin": asyncssh.DEVNULL}


def _known_hosts(host: str, port: int, host_key: Optional[str]):
    if not host_key:
        return None
    spec = host if port == 22 else f"[{host}]:{port}"
    return asyncssh.import_known_hosts(f"{spec} {host_key}\n")


class SSHExecutor(Executor):
    def __init__(self, host: str, port: int, user: str, host_key: Optional[str],
                 client_key: asyncssh.SSHKey):
        self.host, self.port, self.user = host, port, user
        self.host_key = host_key
        self.client_key = client_key
        self._conn: Optional[asyncssh.SSHClientConnection] = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> asyncssh.SSHClientConnection:
        async with self._lock:
            if self._conn is None or self._conn.is_closed():
                self._conn = await asyncio.wait_for(asyncssh.connect(
                    self.host, port=self.port, username=self.user,
                    client_keys=[self.client_key], agent_path=None, password=None,
                    known_hosts=_known_hosts(self.host, self.port, self.host_key),
                    keepalive_interval=30,
                ), 20)
            return self._conn

    async def run(self, cmd: str, input: str = "", timeout: int = 120) -> tuple[int, str]:
        for attempt in (1, 2):
            try:
                conn = await self._connect()
                r = await conn.run(SSH_PATH + cmd, timeout=timeout, stderr=asyncssh.STDOUT,
                                   **_stdin(input))
                code = r.exit_status if r.exit_status is not None else 255
                return code, str(r.stdout or "")
            except asyncssh.TimeoutError:
                raise RemoteError(f"Команда на {self.host} не завершилась за {timeout} с")
            except asyncssh.HostKeyNotVerifiable:
                await self.close()
                raise RemoteError(f"Ключ сервера {self.host} изменился. Если это новый сервер — "
                                  "удалите его в боте и добавьте заново.")
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                await self.close()
                if attempt == 2:
                    raise RemoteError(f"Нет связи с {self.host}: {e or type(e).__name__}") from e
        raise AssertionError("unreachable")

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
        raise RemoteError("Неверный логин или пароль.")
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        raise RemoteError(f"Не удалось подключиться к {host}:{port} — {e or type(e).__name__}. "
                          "Проверьте IP и что сервер включён.")
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
