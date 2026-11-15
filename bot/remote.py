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


# ======================= первый вход на сервер =======================
class KeyNeedsPassphrase(RemoteError):
    """SSH-ключ защищён паролем (passphrase) — нужно спросить его."""


# пользователи, под которыми облака пускают по ключу (AWS, Google Cloud, Azure, Oracle…)
CLOUD_USERS = ("root", "ubuntu", "ec2-user", "admin", "debian", "centos", "rocky", "almalinux", "opc", "azureuser")


def looks_like_key(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith("-----BEGIN") or t.startswith("PuTTY-User-Key-File")


def load_user_key(data: bytes, passphrase: Optional[str] = None) -> asyncssh.SSHKey:
    """Приватный SSH-ключ пользователя (OpenSSH, PEM от AWS, PKCS#8). Понятные ошибки на русском."""
    try:
        return asyncssh.import_private_key(data, passphrase or None)
    except asyncssh.KeyEncryptionError:
        raise RemoteError("Неверный пароль от ключа (passphrase)")
    except (asyncssh.KeyImportError, ValueError, TypeError) as e:
        text = str(e)
        if "Passphrase must be specified" in text:
            raise KeyNeedsPassphrase("Ключ защищён паролем (passphrase)")
        if passphrase and ("decrypt" in text.lower() or "passphrase" in text.lower()):
            raise RemoteError("Неверный пароль от ключа (passphrase)")
        raise RemoteError("Не удалось прочитать ключ. Нужен приватный ключ: файл .pem (AWS) "
                          "или id_rsa / id_ed25519, начинается с «-----BEGIN»")


# Даёт боту вход под root по его ключу. Выполняется от root (напрямую или через sudo).
_ROOT_SETUP = r"""
set -e
umask 077
mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys
grep -qxF {key} /root/.ssh/authorized_keys || echo {key} >> /root/.ssh/authorized_keys
chmod 700 /root/.ssh && chmod 600 /root/.ssh/authorized_keys
# root по ключу запрещён (бывает на облаках) — разрешаем ТОЛЬКО по ключу, вход по паролю не включаем
if sshd -T 2>/dev/null | grep -qix 'permitrootlogin no'; then
  if grep -qi '^Include /etc/ssh/sshd_config.d' /etc/ssh/sshd_config; then
    mkdir -p /etc/ssh/sshd_config.d
    echo 'PermitRootLogin prohibit-password' > /etc/ssh/sshd_config.d/00-vpnbot.conf
  else
    sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/I' /etc/ssh/sshd_config
    grep -qi '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin prohibit-password' >> /etc/ssh/sshd_config
  fi
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null || true
fi
"""


async def bootstrap_ssh(host: str, port: int, user: str, bot_key: asyncssh.SSHKey,
                        password: Optional[str] = None, user_key: Optional[asyncssh.SSHKey] = None,
                        try_users: bool = False) -> str:
    """Первый вход на сервер по паролю ИЛИ по SSH-ключу пользователя.

    Бот добавляет СВОЙ ключ для root и дальше ходит только по нему — пароль и ключ пользователя
    нигде не сохраняются. Если вход не под root (например ubuntu на AWS) — настраивает root через sudo.
    try_users: пользователь не указан явно — пробуем root, ubuntu, ec2-user… по очереди.
    → ключ сервера (host key).
    """
    users = [user] + ([u for u in CLOUD_USERS if u != user] if try_users else [])
    tried = []
    conn = None
    for u in users:
        try:
            conn = await asyncio.wait_for(asyncssh.connect(
                host, port=port, username=u, password=password if user_key is None else None,
                client_keys=[user_key] if user_key is not None else None,
                known_hosts=None, agent_path=None,
            ), 25)
            user = u
            break
        except asyncssh.PermissionDenied:
            tried.append(u)
        except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
            raise conn_error(host, port, e)
    if conn is None:
        who = ", ".join(tried)
        if user_key is not None:
            raise RemoteError(f"Сервер не принял этот ключ (пробовал пользователей: {who}). "
                              "Проверьте, что это ключ от этого сервера")
        raise RemoteError("Неверный пароль — или на сервере запрещён вход по паролю")

    pubkey = public_key_line(bot_key)
    async with conn:
        host_key = conn.get_server_host_key().export_public_key().decode().strip()
        script = _ROOT_SETUP.format(key=shlex.quote(pubkey))
        r = await conn.run("id -u", stdin=asyncssh.DEVNULL)
        if str(r.stdout).strip() == "0":
            r = await conn.run(f"bash -c {shlex.quote(script)}", stdin=asyncssh.DEVNULL)
        elif password and user_key is None:   # sudo спросит пароль пользователя
            r = await conn.run(f"sudo -S -p '' bash -c {shlex.quote(script)}", input=password + "\n")
        else:
            r = await conn.run(f"sudo -n bash -c {shlex.quote(script)}", stdin=asyncssh.DEVNULL)
        if r.exit_status:
            err = str(r.stderr or r.stdout or "").strip()
            if "sudo" in err.lower() or "password" in err.lower():
                raise RemoteError(f"У пользователя {user} нет прав sudo — нужен root или пользователь с sudo")
            raise RemoteError(f"Не удалось настроить доступ: {err[-300:]}")

    # проверка: бот входит под root по своему ключу
    try:
        check = await asyncio.wait_for(asyncssh.connect(
            host, port=port, username="root", client_keys=[bot_key], password=None,
            known_hosts=None, agent_path=None), 25)
        async with check:
            r = await check.run("id -u", stdin=asyncssh.DEVNULL)
            if str(r.stdout).strip() != "0":
                raise RemoteError("Вход под root не работает")
    except asyncssh.PermissionDenied:
        raise RemoteError("Ключ бота добавлен, но сервер не пускает root по ключу. "
                          "Проверьте PermitRootLogin в /etc/ssh/sshd_config")
    except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
        raise conn_error(host, port, e)
    return host_key
