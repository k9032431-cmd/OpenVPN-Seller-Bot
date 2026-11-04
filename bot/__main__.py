"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault

from . import services
from .config import load_config
from .db import DB
from .engine import Fleet
from .handlers import admin_keyboard, build_admin_router, build_user_router
from .scripts_ui import build_scripts_router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    cfg = load_config()
    db = DB(cfg.db_path)
    fleet = Fleet(db, Path(cfg.data_dir) / "ssh_key")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command="start", description="Меню"),
                               BotCommand(command="key", description="Получить мой ключ")],
                              scope=BotCommandScopeDefault())

    dp = Dispatcher()
    dp.include_router(build_scripts_router(cfg))   # мастер скриптов — до общего «меню по умолчанию»
    dp.include_router(build_admin_router(cfg))
    dp.include_router(build_user_router())

    tasks = [asyncio.create_task(services.expiry_loop(bot, db, fleet, cfg)),
             asyncio.create_task(services.watch_loop(bot, db, fleet, cfg))]

    logging.info("Бот @%s запущен: скриптов %d, серверов %d", me.username, len(db.scripts()), len(db.servers()))
    if not db.scripts():
        hint = "Начните с «📜 Скрипты» → «⚡ Готовый: OpenVPN» или «➕ Добавить скрипт»."
    elif not db.servers():
        hint = "Теперь добавьте сервер: «🖥 Серверы» → «➕ Добавить сервер»."
    else:
        hint = f"Серверов: {len(db.servers())}. Нажмите «❓ Как это работает», если впервые."
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
