"""Работа с OpenVPN на сервере (локально или по SSH).

Повторяет логику scripts/openvpn-install.sh (newClient / revokeClient), но без вопросов,
чтобы бот мог делать всё сам.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import shlex
from typing import Optional

from .remote import Executor, RemoteError

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9-]{1,63}\.)+[A-Za-z]{2,63}$")
CERT_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.S)
REMOTE_RE = re.compile(r"^remote[ \t]+(\S+)[ \t]+(\d+)[ \t]*$", re.M)
PROTO_RE = re.compile(r"^proto[ \t]+(\S+)", re.M)
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


class OpenVPN:
    def __init__(self, executor: Executor, base: str = "/etc/openvpn"):
        self.ex = executor
        self.base = base
        self.easyrsa = f"{base}/easy-rsa"
        self.pki = f"{self.easyrsa}/pki"
        self.template = f"{base}/client-template.txt"
        self.lock = asyncio.Lock()

    # ---------- низкий уровень ----------
    async def sh(self, cmd: str, input: str = "", timeout: int = 120, check: bool = True) -> str:
        rc, out = await self.ex.run(cmd, input, timeout)
        if check and rc != 0:
            raise OpenVPNError(f"Ошибка на сервере (код {rc}):\n{out[-1500:]}")
        return out

    async def read_files(self, *paths: str) -> dict[str, Optional[str]]:
        """Читает несколько файлов за одну команду. Нет файла → None."""
        marker = "@@F" + secrets.token_hex(8)
        cmd = "; ".join(
            f"echo {marker}; if [ -f {shlex.quote(p)} ]; then echo OK; cat {shlex.quote(p)}; "
            f"else echo NO; fi" for p in paths
        )
        out = await self.sh(cmd)
        chunks = out.split(marker + "\n")[1:]
        result: dict[str, Optional[str]] = {}
        for p, chunk in zip(paths, chunks):
            status, _, body = chunk.partition("\n")
            result[p] = body if status == "OK" else None
        return result

    async def write_file(self, path: str, data: str) -> None:
        q = shlex.quote(path)
        await self.sh(f"cat > {q}.tmp && mv {q}.tmp {q}", input=data)

    # ---------- состояние ----------
    async def is_installed(self) -> bool:
        rc, _ = await self.ex.run(f"test -f {self.base}/server.conf && test -f {self.template}")
        return rc == 0

    async def is_running(self) -> bool:
        rc, _ = await self.ex.run(
            "systemctl is-active -q openvpn@server || systemctl is-active -q openvpn-server@server"
            " || systemctl is-active -q openvpn")
        return rc == 0

    async def restart(self) -> None:
        await self.sh("systemctl restart openvpn@server 2>/dev/null || "
                      "systemctl restart openvpn-server@server 2>/dev/null || systemctl restart openvpn")

    async def public_ip(self, version: int = 4) -> Optional[str]:
        """Внешний IP, каким его видит сам сервер."""
        v = f"-{version}"
        _, out = await self.ex.run(
            f"curl {v} -s --max-time 8 https://api.ipify.org || "
            f"curl {v} -s --max-time 8 https://ifconfig.me/ip || "
            f"wget {v} -qO- --timeout=8 https://api.ipify.org", timeout=40)
        try:
            ip = ipaddress.ip_address(out.strip())
            return str(ip) if ip.is_global and ip.version == version else None
        except ValueError:
            return None

    async def valid_clients(self) -> list[str]:
        files = await self.read_files(f"{self.pki}/index.txt", f"{self.easyrsa}/SERVER_NAME_GENERATED")
        server = (files[f"{self.easyrsa}/SERVER_NAME_GENERATED"] or "").strip()
        result = []
        for line in (files[f"{self.pki}/index.txt"] or "").splitlines():
            m = CN_RE.search(line)
            if line.startswith("V") and m and m.group(1) != server:
                result.append(m.group(1))
        return result

    # ---------- установка ----------
    async def install(self, script: str, endpoint: str, proto: str = "udp", port: int = 1194) -> None:
        """Ставит OpenVPN вашим скриптом в автоматическом режиме (без вопросов)."""
        await self.sh("cat > /root/openvpn-install.sh && chmod +x /root/openvpn-install.sh", input=script)
        env = {
            "AUTO_INSTALL": "y", "APPROVE_INSTALL": "y", "APPROVE_IP": "y", "CONTINUE": "y",
            "IPV6_SUPPORT": "n", "PORT_CHOICE": "2", "PORT": str(port),
            "PROTOCOL_CHOICE": "1" if proto == "udp" else "2",
            "DNS": "11", "COMPRESSION_ENABLED": "n", "CUSTOMIZE_ENC": "n",
            "CLIENT": "test", "PASS": "1", "ENDPOINT": endpoint,
            "DEBIAN_FRONTEND": "noninteractive",
        }
        env_str = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        out = await self.sh(f"{env_str} bash /root/openvpn-install.sh", timeout=1500, check=False)
        if not await self.is_installed():
            raise OpenVPNError("Установка не удалась. Последние строки:\n" + out[-1200:])

    # ---------- ключи ----------
    async def create_client(self, name: str) -> None:
        if not NAME_RE.match(name):
            raise OpenVPNError("Имя: только латиница, цифры, _ и - (до 32 символов).")
        async with self.lock:
            if name in await self.valid_clients():
                raise OpenVPNError(f"Ключ {name} уже есть на этом сервере.")
            await self.sh(f"cd {self.easyrsa} && ./easyrsa --batch build-client-full "
                          f"{shlex.quote(name)} nopass")

    async def revoke_client(self, name: str) -> bool:
        """Отзывает сертификат. False — если активного сертификата с таким именем нет."""
        async with self.lock:
            if name not in await self.valid_clients():
                return False
            n = shlex.quote(name)
            await self.sh(
                f"set -e; cd {self.easyrsa}; ./easyrsa --batch revoke {n}; "
                f"EASYRSA_CRL_DAYS=3650 ./easyrsa gen-crl; "
                f"cp pki/crl.pem {self.base}/crl.pem.tmp; chmod 644 {self.base}/crl.pem.tmp; "
                f"mv {self.base}/crl.pem.tmp {self.base}/crl.pem; set +e; "
                f"sed -i '/^{name},/d' {self.base}/ipp.txt 2>/dev/null; "
                f"rm -f /root/{n}.ovpn; find /home -maxdepth 2 -name {n}.ovpn -delete 2>/dev/null; "
                f"cp pki/index.txt pki/index.txt.bk; true"
            )
        return True

    async def build_ovpn(self, name: str) -> bytes:
        paths = {
            "tpl": self.template, "ca": f"{self.pki}/ca.crt",
            "cert": f"{self.pki}/issued/{name}.crt", "key": f"{self.pki}/private/{name}.key",
            "conf": f"{self.base}/server.conf",
            "crypt": f"{self.base}/tls-crypt.key", "auth": f"{self.base}/tls-auth.key",
        }
        files = await self.read_files(*paths.values())
        f = {k: files[p] for k, p in paths.items()}
        if not f["cert"] or not f["key"] or not f["tpl"] or not f["ca"]:
            raise OpenVPNError(f"Файлы ключа {name} не найдены на сервере.")
        cert = CERT_RE.search(f["cert"])
        if not cert:
            raise OpenVPNError(f"Не удалось прочитать сертификат {name}.")
        parts = [f["tpl"].rstrip("\n"),
                 "<ca>", f["ca"].strip(), "</ca>",
                 "<cert>", cert.group(0), "</cert>",
                 "<key>", f["key"].strip(), "</key>"]
        conf_lines = (f["conf"] or "").splitlines()
        if any(l.startswith("tls-crypt") for l in conf_lines) and f["crypt"]:
            parts += ["<tls-crypt>", f["crypt"].strip(), "</tls-crypt>"]
        elif any(l.startswith("tls-auth") for l in conf_lines) and f["auth"]:
            parts += ["key-direction 1", "<tls-auth>", f["auth"].strip(), "</tls-auth>"]
        return ("\n".join(parts) + "\n").encode()

    # ---------- адрес в ключах ----------
    async def get_remote(self) -> tuple[str, int, str]:
        """(адрес, порт, протокол) из client-template.txt."""
        tpl = (await self.read_files(self.template))[self.template]
        m = REMOTE_RE.search(tpl or "")
        if not m:
            raise OpenVPNError("OpenVPN не установлен на сервере (нет client-template.txt).")
        p = PROTO_RE.search(tpl)
        proto = "tcp" if p and p.group(1).startswith("tcp") else "udp"
        return m.group(1), int(m.group(2)), proto

    async def set_remote_host(self, host: str) -> None:
        if not is_valid_host(host):
            raise OpenVPNError(f"Некорректный адрес: {host}")
        async with self.lock:
            tpl = (await self.read_files(self.template))[self.template] or ""
            new, n = REMOTE_RE.subn(lambda m: f"remote {host} {m.group(2)}", tpl, count=1)
            if n != 1:
                raise OpenVPNError("В client-template.txt нет строки 'remote <ip> <port>'.")
            await self.sh(f"cp {self.template} {self.template}.bak")
            await self.write_file(self.template, new)


__all__ = ["OpenVPN", "OpenVPNError", "RemoteError", "NAME_RE", "is_ip", "is_valid_host"]
