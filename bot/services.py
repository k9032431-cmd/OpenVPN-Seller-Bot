"""Общая логика: отправка ключей, смена IP, перенос клиентов, фоновые проверки."""
from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import BufferedInputFile

from . import engine, texts
from .config import Config
from .db import DB
from .engine import ERRORS, Fleet, Progress, _noop
from .remote import RemoteError

log = logging.getLogger(__name__)
esc = html.escape


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


async def send_key(bot: Bot, chat_id: int, client, caption: str) -> bool:
    """Ключ — файлом (.ovpn и т.п.) или текстом (vless://…), как вернул скрипт."""
    value = bytes(client["value"] or b"")
    if not value:
        return False
    cap = caption.format(name=client["name"])
    if client["filename"]:
        doc = BufferedInputFile(value, filename=client["filename"])
        return await _retrying(lambda: bot.send_document(chat_id, doc, caption=cap))
    text = f"{cap}\n\n<code>{esc(value.decode(errors='replace'))[:3800]}</code>"
    return await _retrying(lambda: bot.send_message(chat_id, text))


async def push_configs(bot: Bot, db: DB, sid: int, caption: str) -> tuple[int, int]:
    ok = fail = 0
    for c in db.active_bound(sid):
        if await send_key(bot, c["tg_id"], c, caption):
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def notify_dealers(bot: Bot, db: DB, clients, header: str) -> None:
    """Каждому диллеру — сообщение и обновлённые файлы его ключей из списка clients."""
    by_dealer: dict[int, list] = {}
    for c in clients:
        if c["dealer_id"]:
            by_dealer.setdefault(c["dealer_id"], []).append(c)
    for did, items in by_dealer.items():
        d = db.dealer(did)
        if not d or not d["tg_id"]:
            continue
        await safe_send(bot, d["tg_id"], f"{header}\n\nВаших ключей: <b>{len(items)}</b> — обновлённые файлы ниже.")
        for c in items:
            await send_key(bot, d["tg_id"], c, texts.DEALER_KEY_CAPTION)
            await asyncio.sleep(0.05)


async def tell_dealer(bot: Bot, db: DB, client, text: str) -> None:
    d = db.dealer(client["dealer_id"])
    if d and d["tg_id"]:
        await safe_send(bot, d["tg_id"], text)


# ---------- смена IP ----------
async def switch_host(db: DB, fleet: Fleet, sid: int, new_host: str, host_key: Optional[str] = None) -> str:
    """Переключает сервер на новый IP, если бот может туда войти. → старый IP.

    Не вышло — IP остаётся прежним, ошибка пробрасывается: RemoteAuthError / RemoteHostKeyError
    (нужен пароль — похоже, это новый сервер) или RemoteError (не отвечает и т.п.).
    """
    server = db.server(sid)
    old_host, old_key = server["host"], server["host_key"]
    db.set_server_access(sid, new_host, host_key if host_key is not None else old_key)
    fleet.forget(sid)
    try:
        await fleet.remote(db.server(sid)).ping()
    except RemoteError:
        db.set_server_access(sid, old_host, old_key)
        fleet.forget(sid)
        raise
    if server["name"] == old_host:  # сервер был назван по IP — переименуем
        db.rename_server(sid, new_host)
    return old_host


async def missing_vpn(db: DB, fleet: Fleet, sid: int) -> list[str]:
    """Скрипты, которые по базе стоят на сервере, но «Проверка установки» их не находит
    (значит, на этом IP другой — новый — сервер)."""
    server = db.server(sid)
    missing = []
    for script in db.installed_scripts(sid):
        if script["check_cmd"].strip() and not await engine.is_installed(fleet, db, server, script):
            missing.append(script["name"])
    return missing


async def change_address(bot: Bot, db: DB, fleet: Fleet, sid: int, old: str,
                         progress: Progress = _noop) -> tuple[int, int, int, list[str]]:
    """Тот же сервер, новый IP (бот уже подключён): шаги «Смена IP» на сервере → заменить IP во всех
    ключах → разослать клиентам и диллерам. → (обновлено ключей, отправлено, ошибок, предупреждения)."""
    server = db.server(sid)
    new_host = server["host"]
    await progress("✅ Подключение по новому IP работает, IP сохранён")
    warnings = await engine.change_ip_on_server(fleet, db, server, old, new_host, progress)
    updated = 0
    old_b, new_b = old.encode(), new_host.encode()
    for c in db.server_clients(sid):
        value = bytes(c["value"] or b"")
        if old_b in value:
            db.set_value(c["id"], value.replace(old_b, new_b))
            updated += 1
    await progress(f"🔑 Обновлено ключей: {updated}. Рассылаю клиентам и диллерам…")
    ok, fail = await push_configs(bot, db, sid, texts.IP_CHANGED_CAPTION)
    await notify_dealers(bot, db, db.active(sid), texts.DEALER_IP_CHANGED.format(
        server=esc(server["name"]), old=old, new=esc(new_host)))
    return updated, ok, fail, warnings


async def rebuild_server(bot: Bot, db: DB, fleet: Fleet, sid: int,
                         progress: Progress = _noop) -> tuple[int, list[str]]:
    """На месте сервера — новый (например, новый инстанс AWS): поставить VPN заново и пересоздать
    ключи всем активным клиентам с теми же именами и сроками. Клиенты и диллеры получат новые файлы."""
    db.reset_installed(sid)
    server = db.server(sid)
    clients = db.active(sid)
    ok, errors, done = 0, [], []
    for i, c in enumerate(clients, 1):
        script = db.script(c["script_id"]) or next(iter(db.scripts(only_enabled=True)), None)
        if script is None:
            errors.append(f"{c['name']}: нет скрипта")
            continue
        await progress(f"🔑 {i}/{len(clients)}: {c['name']}")
        try:
            value, filename = await engine.create_key(fleet, db, server, script, c["name"], progress)
        except ERRORS as e:
            errors.append(f"{c['name']}: {e}")
            continue
        db.move_client(c["id"], sid, c["name"], value, filename)
        fresh = db.get(c["id"])
        done.append(fresh)
        if fresh["tg_id"]:
            await send_key(bot, fresh["tg_id"], fresh, texts.REBUILT_CAPTION)
        ok += 1
    await notify_dealers(bot, db, done, texts.DEALER_REBUILT.format(server=esc(server["name"])))
    return ok, errors


# ---------- перенос клиентов ----------
async def transfer_clients(bot: Bot, db: DB, fleet: Fleet, src: int, dst: int,
                           progress: Progress = _noop) -> tuple[int, list[str]]:
    """Каждому активному клиенту сервера src — новый ключ на сервере dst (сроки сохраняются)."""
    target = db.server(dst)
    errors = []
    ok = 0
    moved_ids = []
    clients = db.active(src)
    for i, c in enumerate(clients, 1):
        script = db.script(c["script_id"]) or next(iter(db.scripts(only_enabled=True)), None)
        if script is None:
            errors.append(f"{c['name']}: нет скрипта")
            continue
        name, n = c["name"], 2
        while db.name_taken(dst, name):
            name, n = f"{c['name']}-{n}", n + 1
        await progress(f"🔁 {i}/{len(clients)}: {name}")
        try:
            value, filename = await engine.create_key(fleet, db, target, script, name, progress)
        except ERRORS as e:
            errors.append(f"{c['name']}: {e}")
            continue
        try:  # старый ключ удаляем, если старый сервер ещё доступен
            await engine.delete_key(fleet, db, c)
        except ERRORS:
            pass
        db.move_client(c["id"], dst, name, value, filename)
        moved = db.get(c["id"])
        moved_ids.append(c["id"])
        if moved["tg_id"]:
            await send_key(bot, moved["tg_id"], moved, texts.MOVED_CAPTION)
        ok += 1
    await notify_dealers(bot, db, [db.get(i) for i in moved_ids], texts.DEALER_MOVED.format(
        server=esc(target["name"])))
    return ok, errors


async def delete_server(bot: Bot, db: DB, fleet: Fleet, sid: int) -> None:
    server = db.server(sid)
    for c in db.active(sid):
        if c["tg_id"]:
            await safe_send(bot, c["tg_id"], texts.REVOKED_USER.format(name=c["name"]))
        await tell_dealer(bot, db, c, texts.DEALER_KEY_OFF.format(
            name=c["name"], why=f"сервер «{esc(server['name'])}» удалён"))
    fleet.forget(sid)
    db.delete_server(sid)


async def revoke(bot: Bot, db: DB, fleet: Fleet, cid: int, user_text: Optional[str],
                 dealer_why: Optional[str] = None) -> None:
    """Отключает ключ. dealer_why — причина для диллера (если ключ его и отключил не он сам)."""
    c = db.get(cid)
    await engine.delete_key(fleet, db, c)
    db.set_status(cid, "revoked")
    if user_text and c["tg_id"]:
        await safe_send(bot, c["tg_id"], user_text.format(name=esc(c["name"])))
    if dealer_why:
        await tell_dealer(bot, db, c, texts.DEALER_KEY_OFF.format(name=c["name"], why=dealer_why))


# ---------- фоновые задачи ----------
async def watch_loop(bot: Bot, db: DB, fleet: Fleet, cfg: Config) -> None:
    """Раз в минуту проверяет, что серверы на связи. Пропал — пишет админу, что делать."""
    fails: dict[int, int] = {}
    while True:
        for s in db.servers():
            sid = s["id"]
            if sid in fleet.busy:
                continue
            try:
                await fleet.remote(s).ping()
                if fails.get(sid, 0) >= 3:
                    await notify_admins(bot, cfg, texts.SERVER_BACK.format(name=esc(s["name"])))
                fails[sid] = 0
            except asyncio.CancelledError:
                raise
            except RemoteError as e:
                fails[sid] = fails.get(sid, 0) + 1
                if fails[sid] == 3:
                    await notify_admins(bot, cfg, texts.SERVER_DOWN.format(
                        name=esc(s["name"]), host=s["host"], error=esc(str(e))))
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
                    await revoke(bot, db, fleet, c["id"], texts.EXPIRED_USER, dealer_why="закончился срок")
                    await notify_admins(bot, cfg, f"⛔ Ключ <b>{esc(c['name'])}</b> истёк и отключён.")
                except ERRORS as e:  # сервер недоступен — попробуем позже
                    log.warning("Не удалось отключить %s: %s", c["name"], e)
            if cfg.warn_days:
                for c in db.expiring(now + cfg.warn_days * 86400):
                    if c["tg_id"]:
                        await safe_send(bot, c["tg_id"], texts.EXPIRE_WARN.format(
                            name=c["name"], date=fmt_date(c["expires_at"])))
                    await tell_dealer(bot, db, c, texts.DEALER_EXPIRING.format(
                        name=c["name"], date=fmt_date(c["expires_at"])))
                    db.mark_warned(c["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка проверки сроков")
        await asyncio.sleep(300)
