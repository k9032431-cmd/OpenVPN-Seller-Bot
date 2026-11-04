"""Выполнение скриптов на серверах: установка, создание/удаление ключа, смена IP.

Всё делается шагами скрипта (см. steps.py) — бот не знает заранее, какой VPN вы ставите.
"""
from __future__ import annotations

import ipaddress
import posixpath
import re
import shlex
from collections.abc import Awaitable, Callable
from pathlib import Path

from .db import DB
from .remote import RemoteError, Server, load_client_key, public_key_line
from .steps import DIR_SH, ENTER_WORDS, fill, session_output, steps_of

INSTALL_TIMEOUT = 1800
KEY_TIMEOUT = 300
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

Progress = Callable[[str], Awaitable[None]]


class EngineError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        self.output = output


ERRORS = (EngineError, RemoteError)


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_valid_host(value: str) -> bool:
    return is_ip(value) or bool(HOST_RE.match(value))


async def _noop(_: str) -> None:
    pass


class Fleet:
    """SSH-подключения ко всем серверам из базы."""

    def __init__(self, db: DB, key_path: Path):
        self.db = db
        self.client_key = load_client_key(key_path)
        self.pubkey = public_key_line(self.client_key)
        self._cache: dict[int, tuple[tuple, Server]] = {}
        self.busy: set[int] = set()   # серверы, где идёт установка/перенос

    def remote(self, server) -> Server:
        sid = server["id"]
        sig = (server["host"], server["port"], server["user"], server["host_key"])
        cached = self._cache.get(sid)
        if cached and cached[0] == sig:
            return cached[1]
        r = Server(server["host"], server["port"], server["user"], server["host_key"], self.client_key)
        self._cache[sid] = (sig, r)
        return r

    def forget(self, sid: int) -> None:
        self._cache.pop(sid, None)

    async def close(self) -> None:
        for _, r in self._cache.values():
            await r.close()


def placeholders(server, script, name: str = "") -> dict[str, str]:
    file = f"{DIR_SH}/{shlex.quote(script['filename'])}" if script and script["filename"] else ""
    return {"name": name, "server_ip": server["host"], "dir": DIR_SH, "file": file, "script": file}


def stepper(progress: Progress, title: str, total: int):
    async def on_step(i: int, line: str):
        shown = "Enter" if line.strip().lower() in ENTER_WORDS else line
        shown = shown if len(shown) <= 50 else shown[:47] + "…"
        await progress(f"🚀 {title}: шаг {i}/{total} · {shown}")
    return on_step


async def is_installed(fleet: Fleet, db: DB, server, script) -> bool:
    if not script["check_cmd"].strip():
        return db.installed_version(server["id"], script["id"]) == script["version"]
    code, _ = await fleet.remote(server).run(fill(script["check_cmd"], placeholders(server, script)), 60)
    return code == 0


async def install(fleet: Fleet, db: DB, server, script, progress: Progress = _noop,
                  force: bool = False) -> None:
    """Устанавливает скрипт на сервер (один раз). Повторно — только если сменилась версия скрипта."""
    r = fleet.remote(server)
    if not force and db.installed_version(server["id"], script["id"]) == script["version"]:
        return
    values = placeholders(server, script)
    if script["check_cmd"].strip():
        code, _ = await r.run(fill(script["check_cmd"], values), 60)
        if code == 0:
            await progress(f"✅ {script['name']} уже установлен на сервере")
            db.set_installed(server["id"], script["id"], script["version"])
            return
    if script["filename"] and script["content"]:
        await progress(f"📤 Загружаю {script['filename']}…")
        await r.upload(script["filename"], bytes(script["content"]))
    steps = steps_of(script["install_steps"], values)
    if steps:
        await progress(f"🚀 Установка: начинаю ({len(steps)} шаг.)")
        res = await r.session(steps, INSTALL_TIMEOUT, stepper(progress, "Установка", len(steps)))
        if not res.ok:
            raise EngineError(f"Установка: {res.error}", res.tail())
        if script["check_cmd"].strip():
            code, _ = await r.run(fill(script["check_cmd"], values), 60)
            if code != 0:
                raise EngineError("Установка прошла, но «Проверка установки» не подтвердила результат",
                                  res.tail())
    db.set_installed(server["id"], script["id"], script["version"])
    await progress("✅ Установка завершена")


def extract_key(output: str, regex: str) -> str:
    if regex:
        found = re.findall(regex, output, flags=re.M)
        found = [f if isinstance(f, str) else f[0] for f in found]
        return found[-1].strip() if found else ""
    return output.strip()


async def create_key(fleet: Fleet, db: DB, server, script, name: str,
                     progress: Progress = _noop) -> tuple[bytes, str | None]:
    """Ставит скрипт (если ещё нет) и создаёт ключ. → (содержимое ключа, имя файла или None)."""
    r = fleet.remote(server)
    async with r.busy:
        await install(fleet, db, server, script, progress)
        values = placeholders(server, script, name)
        steps = steps_of(script["key_steps"], values)
        if not steps:
            raise EngineError("У скрипта не заданы шаги создания ключа")
        await progress(f"🔑 Создаю ключ {name}…")
        res = await r.session(steps, KEY_TIMEOUT, stepper(progress, "Ключ", len(steps)))
        if not res.ok:
            raise EngineError(f"Создание ключа: {res.error}", res.tail())
        if script["result_type"] == "file":
            path = fill(script["result_path"], values)
            data = await r.read_file(path)
            return data, posixpath.basename(path.rstrip("/")) or f"{name}.txt"
        key = extract_key(session_output(res.output), script["key_regex"])
        if not key:
            raise EngineError("Скрипт не вернул ключ", res.tail())
        return key.encode(), None


async def delete_key(fleet: Fleet, db: DB, client) -> None:
    """Выполняет «Удаление ключа» на сервере. Без шагов — ключ просто убирается из бота."""
    server, script = db.server(client["server_id"]), db.script(client["script_id"])
    if not server or not script:
        return
    steps = steps_of(script["delete_steps"], placeholders(server, script, client["name"]))
    if not steps:
        return
    r = fleet.remote(server)
    async with r.busy:
        res = await r.session(steps, KEY_TIMEOUT)
    if not res.ok:
        raise EngineError(f"Удаление ключа: {res.error}", res.tail())


async def change_ip_on_server(fleet: Fleet, db: DB, server, old: str, new: str,
                              progress: Progress = _noop) -> list[str]:
    """Шаги «Смена IP» всех установленных скриптов. → список предупреждений."""
    warnings = []
    extra = {"old_ip": old, "new_ip": new, "old_ip_re": re.escape(old).replace("/", r"\/")}
    r = fleet.remote(server)
    for script in db.installed_scripts(server["id"]):
        steps = steps_of(script["ip_steps"], {**placeholders(server, script), **extra})
        if not steps:
            warnings.append(f"У скрипта «{script['name']}» не заданы шаги «Смена IP» — новые ключи "
                            "могут создаваться со старым IP (📜 Скрипты → Смена IP)")
            continue
        await progress(f"⏳ {script['name']}: обновляю настройки на сервере…")
        async with r.busy:
            res = await r.session(steps, KEY_TIMEOUT)
        if not res.ok:
            warnings.append(f"{script['name']}: {res.error[:200]}")
    return warnings
