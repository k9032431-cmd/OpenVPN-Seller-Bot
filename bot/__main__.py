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
from .handlers import build_admin_router, build_client_router, build_entry_router, build_staff_router
from .ui import menu_keyboard
from .scripts_ui import build_scripts_router


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    cfg = load_config()
    db = DB(cfg.db_path)
    fleet = Fleet(db, Path(cfg.data_dir) / "ssh_key")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    await bot.set_my_commands([BotCommand(command="start", description="🏠 Главное меню")],
                              scope=BotCommandScopeDefault())

    dp = Dispatcher()
    dp.include_router(build_entry_router(cfg))     # /start, «🏠 Главное меню», ссылки — для всех
    dp.include_router(build_scripts_router(cfg))   # 📜 Скрипты — только главный админ
    dp.include_router(build_admin_router(cfg))     # серверы, диллеры, рассылка — только главный админ
    dp.include_router(build_staff_router(cfg))     # ключи — админ и диллеры
    dp.include_router(build_client_router(cfg))    # клиенты

    tasks = [asyncio.create_task(services.expiry_loop(bot, db, fleet, cfg)),
             asyncio.create_task(services.watch_loop(bot, db, fleet, cfg))]

    logging.info("Бот @%s запущен: скриптов %d, серверов %d", me.username, len(db.scripts()), len(db.servers()))
    await services.notify_admins(bot, cfg, "✅ <b>Бот запущен</b>\nНажмите «🏠 Главное меню» внизу.",
                                 reply_markup=menu_keyboard())
    try:
        await dp.start_polling(bot, db=db, fleet=fleet, cfg=cfg, bot_username=me.username)
    finally:
        for t in tasks:
            t.cancel()
        await fleet.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
