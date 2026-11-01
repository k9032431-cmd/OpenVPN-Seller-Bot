"""Определение внешнего (публичного) IP сервера."""
from __future__ import annotations

import ipaddress
import logging
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

SERVICES = {
    4: ("https://api.ipify.org", "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip", "https://ip4.seeip.org"),
    6: ("https://api6.ipify.org", "https://ipv6.icanhazip.com", "https://ip6.seeip.org"),
}


async def detect_public_ip(version: int = 4, timeout: int = 10) -> Optional[str]:
    """Возвращает публичный IP или None, если ни один сервис не ответил."""
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    headers = {"User-Agent": "curl/8"}
    async with aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session:
        for url in SERVICES[version]:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        continue
                    ip = ipaddress.ip_address((await resp.text()).strip())
                    if ip.version == version and ip.is_global:
                        return str(ip)
            except Exception as e:  # сеть, таймаут, мусор в ответе — пробуем следующий
                log.debug("IP-сервис %s не ответил: %s", url, e)
    return None
