"""Работа с OpenVPN, установленным скриптом scripts/openvpn-install.sh.

Повторяет логику функций newClient / revokeClient из скрипта, но без
интерактивных вопросов, чтобы бот мог вызывать их сам.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import shutil
from pathlib import Path
from typing import Optional

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)
REMOTE_RE = re.compile(r"^remote[ \t]+(\S+)[ \t]+(\d+)[ \t]*$", re.M)
CN_RE = re.compile(r"/CN=([^/\s]+)")


class OpenVPNError(RuntimeError):
    pass


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def is_valid_host(value: str) -> bool:
    return is_ip(value) or bool(HOST_RE.match(value))


def _atomic_write(path: Path, data: str, mode: Optional[int] = None) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


class OpenVPN:
    def __init__(self, openvpn_dir: str, easyrsa_dir: str):
        self.dir = Path(openvpn_dir)
        self.easyrsa = Path(easyrsa_dir)
        self.pki = self.easyrsa / "pki"
        self.template = self.dir / "client-template.txt"
        self.server_conf = self.dir / "server.conf"
        self.lock = asyncio.Lock()

    # ---------- проверки ----------
    def check(self) -> None:
        missing = [
            str(p)
            for p in (self.template, self.server_conf, self.easyrsa / "easyrsa", self.pki / "ca.crt")
            if not p.exists()
        ]
        if missing:
            raise OpenVPNError(
                "Не найдена установка OpenVPN. Сначала запустите scripts/openvpn-install.sh.\n"
                "Отсутствуют: " + ", ".join(missing)
            )

    def server_name(self) -> Optional[str]:
        f = self.easyrsa / "SERVER_NAME_GENERATED"
        return f.read_text().strip() if f.exists() else None

    def _index(self) -> list[tuple[str, str]]:
        path = self.pki / "index.txt"
        if not path.exists():
            return []
        result = []
        for line in path.read_text().splitlines():
            m = CN_RE.search(line)
            if line and m:
                result.append((line[0], m.group(1)))
        return result

    def valid_clients(self) -> list[str]:
        server = self.server_name()
        return [cn for status, cn in self._index() if status == "V" and cn != server]

    def is_valid_client(self, name: str) -> bool:
        return name in self.valid_clients()

    # ---------- easy-rsa ----------
    async def _easyrsa(self, *args: str, env: Optional[dict] = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "./easyrsa", "--batch", *args,
            cwd=str(self.easyrsa),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})},
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        if proc.returncode != 0:
            raise OpenVPNError(f"easyrsa {' '.join(args)}: ошибка\n{text[-1500:]}")
        return text

    async def create_client(self, name: str) -> None:
        if not NAME_RE.match(name):
            raise OpenVPNError("Имя может содержать только латиницу, цифры, _ и - (до 32 символов).")
        async with self.lock:
            if self.is_valid_client(name):
                raise OpenVPNError(
                    f"Сертификат {name} уже есть в easy-rsa. Используйте /sync, чтобы добавить его в бота."
                )
            await self._easyrsa("build-client-full", name, "nopass")

    async def revoke_client(self, name: str) -> bool:
        """Отзывает сертификат. False — если активного сертификата с таким именем нет."""
        async with self.lock:
            if not self.is_valid_client(name):
                return False
            await self._easyrsa("revoke", name)
            await self._easyrsa("gen-crl", env={"EASYRSA_CRL_DAYS": "3650"})
            crl_tmp = self.dir / "crl.pem.tmp"
            shutil.copyfile(self.pki / "crl.pem", crl_tmp)
            os.chmod(crl_tmp, 0o644)
            os.replace(crl_tmp, self.dir / "crl.pem")

            ipp = self.dir / "ipp.txt"
            if ipp.exists():
                lines = [l for l in ipp.read_text().splitlines(True) if not l.startswith(f"{name},")]
                ipp.write_text("".join(lines))
            for f in [Path("/root") / f"{name}.ovpn", *Path("/home").glob(f"*/{name}.ovpn")]:
                f.unlink(missing_ok=True)
            shutil.copyfile(self.pki / "index.txt", self.pki / "index.txt.bk")
        return True

    # ---------- .ovpn ----------
    def _tls_sig(self) -> Optional[str]:
        for line in self.server_conf.read_text().splitlines():
            if line.startswith("tls-crypt"):
                return "crypt"
            if line.startswith("tls-auth"):
                return "auth"
        return None

    def build_ovpn(self, name: str) -> bytes:
        cert_file = self.pki / "issued" / f"{name}.crt"
        key_file = self.pki / "private" / f"{name}.key"
        if not cert_file.exists() or not key_file.exists():
            raise OpenVPNError(f"Файлы сертификата {name} не найдены.")
        cert = CERT_RE.search(cert_file.read_text())
        if not cert:
            raise OpenVPNError(f"Не удалось прочитать сертификат {name}.")

        parts = [
            self.template.read_text().rstrip("\n"),
            "<ca>", (self.pki / "ca.crt").read_text().strip(), "</ca>",
            "<cert>", cert.group(0), "</cert>",
            "<key>", key_file.read_text().strip(), "</key>",
        ]
        sig = self._tls_sig()
        if sig == "crypt":
            parts += ["<tls-crypt>", (self.dir / "tls-crypt.key").read_text().strip(), "</tls-crypt>"]
        elif sig == "auth":
            parts += ["key-direction 1", "<tls-auth>",
                      (self.dir / "tls-auth.key").read_text().strip(), "</tls-auth>"]
        return ("\n".join(parts) + "\n").encode()

    # ---------- адрес сервера ----------
    def get_remote(self) -> tuple[str, int]:
        m = REMOTE_RE.search(self.template.read_text())
        if not m:
            raise OpenVPNError(f"В {self.template} нет строки 'remote <ip> <port>'.")
        return m.group(1), int(m.group(2))

    def set_remote_host(self, host: str) -> None:
        if not is_valid_host(host):
            raise OpenVPNError(f"Некорректный адрес: {host}")
        text = self.template.read_text()
        backup = self.template.with_name(self.template.name + ".bak")
        shutil.copyfile(self.template, backup)
        new_text, n = REMOTE_RE.subn(lambda m: f"remote {host} {m.group(2)}", text, count=1)
        if n != 1:
            raise OpenVPNError(f"В {self.template} нет строки 'remote <ip> <port>'.")
        _atomic_write(self.template, new_text)
