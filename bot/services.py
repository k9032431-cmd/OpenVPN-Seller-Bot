"""Общая логика: отправка ключей, смена IP, отзыв, фоновые задачи."""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from . import texts
from .config import Config
from .db import DB
from .ip_detect import detect_public_ip
from .openvpn import OpenVPN, OpenVPNError, is_ip

log = logging.getLogger(__name__)


def fmt_date(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def fmt_until(ts: Optional[int]) -> str:
    return texts.SUB_FOREVER if not ts else texts.SUB_UNTIL.format(date=fmt_date(ts))


async def _retrying(coro_factory) -> bool:
    for _ in range(3):
        try:
            await coro_factory()
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            log.warning("Не удалось отправить: %s", e)
            return False
        except Exception:
            log.exception("Ошибка отправки")
            await asyncio.sleep(2)
    return False


async def safe_send(bot: Bot, chat_id: int, text: str) -> bool:
    return await _retrying(lambda: bot.send_message(chat_id, text))


async def send_config(bot: Bot, ovpn: OpenVPN, chat_id: int, name: str, caption: str) -> bool:
    try:
        data = ovpn.build_ovpn(name)
    except (OpenVPNError, OSError) as e:
        log.error("Не удалось собрать %s.ovpn: %s", name, e)
        return False
    doc = BufferedInputFile(data, filename=f"{name}.ovpn")
    return await _retrying(
        lambda: bot.send_document(chat_id, doc, caption=caption.format(name=name))
    )


async def notify_admins(bot: Bot, cfg: Config, text: str) -> None:
    for admin in cfg.admin_ids:
        await safe_send(bot, admin, text)


async def push_configs(bot: Bot, db: DB, ovpn: OpenVPN, caption: str) -> tuple[int, int]:
    ok = fail = 0
    for c in db.active_bound():
        if await send_config(bot, ovpn, c["tg_id"], c["name"], caption):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)  # не упираться в лимиты Telegram
    return ok, fail


async def apply_new_host(bot: Bot, db: DB, ovpn: OpenVPN, cfg: Config,
                         new_host: str, reason: str) -> tuple[str, int, int]:
    """Меняет адрес в client-template.txt и рассылает новые ключи всем клиентам."""
    async with ovpn.lock:
        old_host, _ = ovpn.get_remote()
        ovpn.set_remote_host(new_host)
    db.set_setting("last_host", new_host)
    log.info("Адрес сервера: %s -> %s (%s)", old_host, new_host, reason)
    await notify_admins(
        bot, cfg,
        f"🌐 Адрес сервера изменён ({reason}): <code>{old_host}</code> → "
        f"<code>{new_host}</code>\nРассылаю новые ключи клиентам…",
    )
    ok, fail = await push_configs(bot, db, ovpn, texts.IP_CHANGED_CAPTION)
    await notify_admins(bot, cfg, f"✅ Новые ключи разосланы: {ok}, ошибок: {fail}.")
    return old_host, ok, fail


async def revoke(bot: Bot, db: DB, ovpn: OpenVPN, name: str, user_text: Optional[str]) -> bool:
    """Отзывает сертификат и помечает ключ в базе. Возвращает True, если был активный сертификат."""
    had_cert = await ovpn.revoke_client(name)
    row = db.get(name)
    if row is not None:
        db.set_status(name, "revoked")
        if user_text and row["tg_id"]:
            await safe_send(bot, row["tg_id"], user_text.format(name=html.escape(name)))
    return had_cert


def sync_from_easyrsa(db: DB, ovpn: OpenVPN) -> tuple[list[str], list[str]]:
    """Подтягивает ключи, созданные скриптом вручную, и помечает отозванные вне бота."""
    valid = set(ovpn.valid_clients())
    added = []
    for name in sorted(valid):
        row = db.get(name)
        if row is None or row["status"] != "active":
            db.add_client(name, note="создан скриптом")
            added.append(name)
    gone = [c["name"] for c in db.active() if c["name"] not in valid]
    for name in gone:
        db.set_status(name, "revoked")
    return added, gone


# ---------- фоновые задачи ----------
async def ip_watch_loop(bot: Bot, db: DB, ovpn: OpenVPN, cfg: Config) -> None:
    """Каждые IP_CHECK_INTERVAL секунд сверяет реальный IP с адресом в ключах."""
    pending: Optional[str] = None
    hits = 0
    while True:
        try:
            host, _ = ovpn.get_remote()
            if is_ip(host):  # если в ключах домен — IP следить не нужно
                current = ipaddress.ip_address(host)
                detected = await detect_public_ip(current.version)
                if detected and ipaddress.ip_address(detected) != current:
                    hits = hits + 1 if detected == pending else 1
                    pending = detected
                    log.info("Обнаружен новый IP %s (%d/%d)", detected, hits, cfg.ip_confirm_checks)
                    if hits >= cfg.ip_confirm_checks:
                        await apply_new_host(bot, db, ovpn, cfg, detected, "автоматически")
                        pending, hits = None, 0
                elif detected:
                    pending, hits = None, 0
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка проверки IP")
        await asyncio.sleep(cfg.ip_check_interval)


async def expiry_loop(bot: Bot, db: DB, ovpn: OpenVPN, cfg: Config) -> None:
    """Отключает просроченные ключи и заранее предупреждает клиентов."""
    while True:
        try:
            now = int(time.time())
            for c in db.expired(now):
                await revoke(bot, db, ovpn, c["name"], texts.EXPIRED_USER)
                await notify_admins(bot, cfg, f"⛔ Ключ <b>{c['name']}</b> истёк и отключён.")
            if cfg.warn_days:
                for c in db.expiring(now + cfg.warn_days * 86400):
                    if c["tg_id"]:
                        await safe_send(bot, c["tg_id"], texts.EXPIRE_WARN.format(
                            name=c["name"], date=fmt_date(c["expires_at"])))
                    db.mark_warned(c["name"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка проверки сроков")
        await asyncio.sleep(300)
