"""«Парк» серверов: по записи из базы выдаёт объект OpenVPN для работы с сервером."""
from __future__ import annotations

import asyncio
from pathlib import Path

from .db import DB
from .openvpn import OpenVPN
from .remote import LocalExecutor, SSHExecutor, load_client_key, public_key_line

INSTALL_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "openvpn-install.sh"


class Fleet:
    def __init__(self, db: DB, key_path: Path):
        self.db = db
        self.client_key = load_client_key(key_path)
        self.pubkey = public_key_line(self.client_key)
        self._cache: dict[int, tuple[tuple, OpenVPN]] = {}
        self.busy: set[int] = set()   # серверы, где сейчас идёт установка/перенос

    def ovpn(self, server) -> OpenVPN:
        sid = server["id"]
        sig = (server["kind"], server["host"], server["port"], server["user"], server["host_key"])
        cached = self._cache.get(sid)
        if cached and cached[0] == sig:
            return cached[1]
        if cached:  # адрес сервера поменялся — закрываем старое соединение
            asyncio.ensure_future(cached[1].ex.close())
        if server["kind"] == "local":
            ex = LocalExecutor()
        else:
            ex = SSHExecutor(server["host"], server["port"], server["user"],
                             server["host_key"], self.client_key)
        o = OpenVPN(ex)
        self._cache[sid] = (sig, o)
        return o

    def by_id(self, sid: int) -> OpenVPN:
        server = self.db.server(sid)
        if server is None:
            raise KeyError(sid)
        return self.ovpn(server)

    def forget(self, sid: int) -> None:
        cached = self._cache.pop(sid, None)
        if cached:
            asyncio.ensure_future(cached[1].ex.close())

    @staticmethod
    def install_script() -> str:
        return INSTALL_SCRIPT.read_text()

    async def close(self) -> None:
        for _, o in self._cache.values():
            await o.ex.close()
