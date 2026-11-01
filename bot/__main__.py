"""Точка входа: python -m bot"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

from . import services
from .config import load_config
from .db import DB
from .handlers import admin_keyboard, build_admin_router, build_user_router
from .openvpn import OpenVPN

USER_COMMANDS = [
    BotCommand(command="start", description="Меню"),
    BotCommand(command="key", description="Получить мой ключ"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="new", description="Создать ключ: имя [дней] [заметка]"),
    BotCommand(command="list", description="Все ключи"),
    BotCommand(command="info", description="Инфо о ключе"),
    BotCommand(command="get", description="Получить .ovpn"),
    BotCommand(command="link", description="Новая ссылка для ключа"),
    BotCommand(command="extend", description="Продлить: имя дней"),
    BotCommand(command="revoke", description="Отключить ключ"),
    BotCommand(command="sync", description="Импорт ключей из easy-rsa"),
    BotCommand(command="ip", description="IP сервера"),
    BotCommand(command="setip", description="Сменить адрес и разослать ключи"),
    BotCommand(command="push", description="Разослать ключи всем"),
    BotCommand(command="broadcast", description="Сообщение всем клиентам"),
    BotCommand(command="help", description="Помощь"),
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    ovpn = OpenVPN(cfg.openvpn_dir, cfg.easyrsa_dir)
    ovpn.check()
    db = DB(cfg.db_path)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
    for admin in cfg.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin))
        except Exception as e:  # админ ещё не писал боту
            logging.warning("Не удалось задать команды для %s: %s", admin, e)

    dp = Dispatcher()
    dp.include_router(build_admin_router(cfg))
    dp.include_router(build_user_router())

    tasks = [asyncio.create_task(services.expiry_loop(bot, db, ovpn, cfg))]
    if cfg.ip_auto_update:
        tasks.append(asyncio.create_task(services.ip_watch_loop(bot, db, ovpn, cfg)))

    added, _ = services.sync_from_easyrsa(db, ovpn)
    host, port = ovpn.get_remote()
    logging.info("Бот @%s запущен. Адрес в ключах: %s:%s", me.username, host, port)
    for admin in cfg.admin_ids:
        try:
            await bot.send_message(
                admin,
                f"✅ Бот запущен. Адрес сервера в ключах: <code>{host}:{port}</code>\n"
                + (f"Найдено ключей, созданных скриптом: {len(added)}\n" if added else "")
                + "Нажмите «❓ Как это работает», если впервые.",
                reply_markup=admin_keyboard(),
            )
        except Exception as e:
            logging.warning("Не удалось написать админу %s: %s", admin, e)
    try:
        await dp.start_polling(bot, db=db, ovpn=ovpn, cfg=cfg, bot_username=me.username)
    finally:
        for t in tasks:
            t.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
