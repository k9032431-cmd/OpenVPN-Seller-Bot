"""Общая логика: ключи, смена IP, установка, перенос клиентов, фоновые проверки."""
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
from .fleet import Fleet
from .openvpn import OpenVPNError, is_ip
from .remote import RemoteError

log = logging.getLogger(__name__)
ERRORS = (OpenVPNError, RemoteError)


def fmt_date(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def fmt_until(ts: Optional[int]) -> str:
    return texts.SUB_FOREVER if not ts else texts.SUB_UNTIL.format(date=fmt_date(ts))


# ---------- отправка в Telegram ----------
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


async def safe_send(bot: Bot, chat_id: int, text: str, **kw) -> bool:
    return await _retrying(lambda: bot.send_message(chat_id, text, **kw))


async def notify_admins(bot: Bot, cfg: Config, text: str, **kw) -> None:
    for admin in cfg.admin_ids:
        await safe_send(bot, admin, text, **kw)


async def config_file(fleet: Fleet, client) -> BufferedInputFile:
    data = await fleet.by_id(client["server_id"]).build_ovpn(client["name"])
    return BufferedInputFile(data, filename=f"{client['name']}.ovpn")


async def send_config(bot: Bot, fleet: Fleet, chat_id: int, client, caption: str) -> bool:
    try:
        doc = await config_file(fleet, client)
    except (*ERRORS, KeyError) as e:
        log.error("Не удалось собрать %s.ovpn: %s", client["name"], e)
        return False
    return await _retrying(lambda: bot.send_document(
        chat_id, doc, caption=caption.format(name=client["name"])))


async def push_configs(bot: Bot, db: DB, fleet: Fleet, sid: int, caption: str) -> tuple[int, int]:
    ok = fail = 0
    for c in db.active_bound(sid):
        if await send_config(bot, fleet, c["tg_id"], c, caption):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


# ---------- серверы ----------
def sync_server(db: DB, sid: int, valid: list[str]) -> tuple[list[str], list[str]]:
    """Подтягивает в бота ключи, созданные скриптом вручную; помечает отозванные вне бота."""
    added = []
    for name in sorted(set(valid)):
        row = db.get_by_name(sid, name)
        if row is None or row["status"] != "active":
            db.add_client(sid, name, note="создан скриптом")
            added.append(name)
    gone = [c for c in db.active(sid) if c["name"] not in valid]
    for c in gone:
        db.set_status(c["id"], "revoked")
    return added, [c["name"] for c in gone]


async def sync(db: DB, fleet: Fleet, sid: int) -> tuple[list[str], list[str]]:
    o = fleet.by_id(sid)
    if not await o.is_installed():
        return [], []
    return sync_server(db, sid, await o.valid_clients())


async def change_address(bot: Bot, db: DB, fleet: Fleet, cfg: Config, sid: int,
                         new_host: str, reason: str, move_ssh: bool) -> tuple[int, int]:
    """Новый адрес сервера → правит ключи на сервере → рассылает их клиентам этого сервера.

    move_ssh=True: сервер переехал на новый IP — бот подключается к нему уже по новому адресу.
    """
    server = db.server(sid)
    old_ssh = server["host"]
    if move_ssh and server["kind"] == "ssh":
        db.set_server_host(sid, new_host)
        try:
            await fleet.by_id(sid).is_installed()  # проверка связи по новому адресу
        except RemoteError:
            db.set_server_host(sid, old_ssh)
            fleet.forget(sid)
            raise
    o = fleet.by_id(sid)
    old_remote, port, _ = await o.get_remote()
    await o.set_remote_host(new_host)
    log.info("Сервер %s: адрес %s -> %s (%s)", server["name"], old_remote, new_host, reason)
    await notify_admins(bot, cfg,
                        f"🌐 <b>{html.escape(server['name'])}</b>: адрес изменён ({reason})\n"
                        f"<code>{old_remote}</code> → <code>{new_host}</code>\n"
                        "Рассылаю новые ключи клиентам…")
    ok, fail = await push_configs(bot, db, fleet, sid, texts.IP_CHANGED_CAPTION)
    await notify_admins(bot, cfg, f"✅ Новые ключи разосланы: {ok}, не доставлено: {fail}.")
    return ok, fail


async def install_openvpn(db: DB, fleet: Fleet, sid: int, proto: str, port: int) -> list[str]:
    server = db.server(sid)
    o = fleet.ovpn(server)
    endpoint = server["host"]
    if server["kind"] == "local" or not is_ip(endpoint):
        endpoint = await o.public_ip() or endpoint
    await o.install(fleet.install_script(), endpoint, proto, port)
    added, _ = sync_server(db, sid, await o.valid_clients())
    return added


async def transfer_clients(bot: Bot, db: DB, fleet: Fleet, src: int, dst: int) -> tuple[int, int]:
    """Создаёт всем клиентам сервера src новые ключи на сервере dst и рассылает их."""
    target = fleet.by_id(dst)
    existing = set(await target.valid_clients())
    source = fleet.by_id(src)
    ok = fail = 0
    for c in db.active(src):
        name = c["name"]
        n = 2
        while name in existing or db.get_by_name(dst, name):
            name, n = f"{c['name']}-{n}", n + 1
        try:
            await target.create_client(name)
        except ERRORS as e:
            log.error("Перенос %s: %s", c["name"], e)
            fail += 1
            continue
        existing.add(name)
        try:  # старый ключ отключаем, если старый сервер ещё доступен
            await source.revoke_client(c["name"])
        except ERRORS:
            pass
        db.move_client(c["id"], dst, name)
        moved = db.get(c["id"])
        if moved["tg_id"]:
            await send_config(bot, fleet, moved["tg_id"], moved, texts.MOVED_CAPTION)
        ok += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def delete_server(bot: Bot, db: DB, fleet: Fleet, sid: int) -> None:
    for c in db.active(sid):
        if c["tg_id"]:
            await safe_send(bot, c["tg_id"], texts.REVOKED_USER.format(name=c["name"]))
    fleet.forget(sid)
    db.delete_server(sid)


async def revoke(bot: Bot, db: DB, fleet: Fleet, cid: int, user_text: Optional[str]) -> None:
    c = db.get(cid)
    await fleet.by_id(c["server_id"]).revoke_client(c["name"])
    db.set_status(cid, "revoked")
    if user_text and c["tg_id"]:
        await safe_send(bot, c["tg_id"], user_text.format(name=html.escape(c["name"])))


# ---------- фоновые задачи ----------
async def watch_loop(bot: Bot, db: DB, fleet: Fleet, cfg: Config) -> None:
    """Каждую минуту проверяет все серверы: доступен ли, не сменился ли внешний IP."""
    pending: dict[int, tuple[str, int]] = {}
    fails: dict[int, int] = {}
    while True:
        for s in db.servers():
            sid = s["id"]
            if sid in fleet.busy:
                continue
            try:
                o = fleet.ovpn(s)
                installed = await o.is_installed()
                if fails.get(sid, 0) >= 3:
                    await notify_admins(bot, cfg, texts.SERVER_BACK.format(name=html.escape(s["name"])))
                fails[sid] = 0
                # Сам IP отслеживаем только там, где это надёжно: на сервере с ботом или если
                # к серверу подключаемся по домену. Иначе у VPS с несколькими IP бот мог бы
                # ошибочно подставить «исходящий» адрес. Для остальных — кнопка «Изменить IP».
                auto = s["kind"] == "local" or not is_ip(s["host"])
                if not installed or not cfg.ip_auto_update or not auto:
                    continue
                host, _, _ = await o.get_remote()
                if not is_ip(host):
                    continue  # в ключах домен — IP следить не нужно
                current = ipaddress.ip_address(host)
                detected = await o.public_ip(current.version)
                if detected and ipaddress.ip_address(detected) != current:
                    prev, hits = pending.get(sid, ("", 0))
                    hits = hits + 1 if prev == detected else 1
                    pending[sid] = (detected, hits)
                    if hits >= cfg.ip_confirm_checks:
                        pending.pop(sid, None)
                        await change_address(bot, db, fleet, cfg, sid, detected,
                                             "автоматически", move_ssh=False)
                elif detected:
                    pending.pop(sid, None)
            except asyncio.CancelledError:
                raise
            except RemoteError as e:
                fails[sid] = fails.get(sid, 0) + 1
                if fails[sid] == 3:
                    await notify_admins(bot, cfg, texts.SERVER_DOWN.format(
                        name=html.escape(s["name"]), host=s["host"], error=html.escape(str(e))))
            except Exception:
                log.exception("Ошибка проверки сервера %s", s["name"])
        await asyncio.sleep(cfg.ip_check_interval)


async def expiry_loop(bot: Bot, db: DB, fleet: Fleet, cfg: Config) -> None:
    """Отключает просроченные ключи и заранее предупреждает клиентов."""
    while True:
        try:
            now = int(time.time())
            for c in db.expired(now):
                try:
                    await revoke(bot, db, fleet, c["id"], texts.EXPIRED_USER)
                    await notify_admins(bot, cfg, f"⛔ Ключ <b>{c['name']}</b> истёк и отключён.")
                except (*ERRORS, KeyError) as e:  # сервер недоступен — попробуем позже
                    log.warning("Не удалось отключить %s: %s", c["name"], e)
            if cfg.warn_days:
                for c in db.expiring(now + cfg.warn_days * 86400):
                    if c["tg_id"]:
                        await safe_send(bot, c["tg_id"], texts.EXPIRE_WARN.format(
                            name=c["name"], date=fmt_date(c["expires_at"])))
                    db.mark_warned(c["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка проверки сроков")
        await asyncio.sleep(300)
