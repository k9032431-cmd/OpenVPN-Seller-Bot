"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from . import services
from .config import load_config
from .db import DB
from .fleet import Fleet
from .handlers import admin_keyboard, build_admin_router, build_user_router
from .openvpn import OpenVPN
from .remote import LocalExecutor


async def register_local_server(db: DB) -> None:
    """Если OpenVPN стоит на этом же сервере, что и бот, — добавить его автоматически."""
    if db.local_server() is not None or not os.path.exists("/etc/openvpn/server.conf"):
        return
    o = OpenVPN(LocalExecutor())
    ip = await o.public_ip() or "localhost"
    sid = db.add_server("Этот сервер", "local", ip)
    db.assign_orphans(sid)  # ключи из прошлой версии бота
    logging.info("Добавлен локальный сервер (%s)", ip)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    cfg = load_config()
    db = DB(cfg.db_path)
    await register_local_server(db)
    fleet = Fleet(db, Path(cfg.data_dir) / "ssh_key")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command="start", description="Меню"),
                               BotCommand(command="key", description="Получить мой ключ")],
                              scope=BotCommandScopeDefault())

    dp = Dispatcher()
    dp.include_router(build_admin_router(cfg))
    dp.include_router(build_user_router())

    for s in db.servers():
        try:
            await services.sync(db, fleet, s["id"])
        except services.ERRORS as e:
            logging.warning("Сервер %s недоступен: %s", s["name"], e)

    tasks = [asyncio.create_task(services.expiry_loop(bot, db, fleet, cfg)),
             asyncio.create_task(services.watch_loop(bot, db, fleet, cfg))]

    n = len(db.servers())
    logging.info("Бот @%s запущен, серверов: %d", me.username, n)
    hint = ("Начните с «🖥 Серверы» → «➕ Добавить сервер»." if n == 0
            else f"Серверов: {n}. Нажмите «❓ Как это работает», если впервые.")
    await services.notify_admins(bot, cfg, f"✅ Бот запущен. {hint}", reply_markup=admin_keyboard())
    try:
        await dp.start_polling(bot, db=db, fleet=fleet, cfg=cfg, bot_username=me.username)
    finally:
        for t in tasks:
            t.cancel()
        await fleet.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
