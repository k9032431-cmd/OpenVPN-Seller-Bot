"""Интерфейс бота.

Главная идея — «панель»: в чате у каждого пользователя одно сообщение бота, которое меняется
при навигации (а не новое сообщение на каждое нажатие). Ответы пользователя в мастерах
(имя ключа, IP, пароль…) бот удаляет и обновляет ту же панель — чат остаётся чистым.

Оформление экранов одинаковое везде:
    <b>Заголовок</b>
    (пустая строка)
    Поле: <b>значение</b>
    (пустая строка)
    <i>подсказка</i>
Кнопки — максимум по две в ряд, внизу «⬅️ Назад · 🏠 Меню».
"""
from __future__ import annotations

import html
import time
from typing import Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup)

esc = html.escape

BTN_HOME = "🏠 Главное меню"
PANELS: dict[int, int] = {}       # chat_id → message_id текущей панели
GREETINGS: dict[int, int] = {}    # chat_id → message_id приветствия с нижней кнопкой


# ======================= разметка =======================
def screen(title: str, *blocks: Optional[str], hint: str = "") -> str:
    """Экран: жирный заголовок, блоки через пустую строку, подсказка курсивом внизу."""
    parts = [f"<b>{title}</b>"] + [b for b in blocks if b]
    if hint:
        parts.append(f"<i>{hint}</i>")
    return "\n\n".join(parts)


def fields(*pairs) -> str:
    """Поля «Название: <b>значение</b>». Пары с None пропускаются."""
    return "\n".join(f"{k}: <b>{v}</b>" for k, v in pairs if v is not None)


def ikb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row
    ])


def nav(back: Optional[str] = None) -> list[tuple[str, str]]:
    """Нижний ряд кнопок: «⬅️ Назад» (если есть куда) и «🏠 Меню»."""
    return [("⬅️ Назад", back), ("🏠 Меню", "home")] if back else [("🏠 Меню", "home")]


def pairs(buttons: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Раскладывает кнопки по две в ряд."""
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


CANCEL = [("✖️ Отмена", "cancel")]


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_HOME)]], resize_keyboard=True,
                               is_persistent=True)


# ======================= панель =======================
async def _delete(bot: Bot, chat_id: int, message_id: Optional[int]) -> None:
    if message_id:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


async def open_panel(bot: Bot, chat_id: int, text: str, kb=None) -> Message:
    """Новая панель внизу чата; предыдущая удаляется."""
    old = PANELS.get(chat_id)
    msg = await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
    PANELS[chat_id] = msg.message_id
    if old and old != msg.message_id:
        await _delete(bot, chat_id, old)
    return msg


async def show(event: Union[CallbackQuery, Message], text: str, kb=None) -> Message:
    """Показать экран.

    Нажатие кнопки → меняем сообщение с этой кнопкой.
    Текст от пользователя → удаляем его и меняем текущую панель (или открываем новую).
    """
    if isinstance(event, CallbackQuery):
        msg = event.message
        try:
            await msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            PANELS[msg.chat.id] = msg.message_id
            return msg
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return msg
            return await open_panel(event.bot, msg.chat.id, text, kb)
    chat_id = event.chat.id
    await _delete(event.bot, chat_id, event.message_id)
    pid = PANELS.get(chat_id)
    if pid:
        try:
            res = await event.bot.edit_message_text(text, chat_id=chat_id, message_id=pid, reply_markup=kb,
                                                    disable_web_page_preview=True)
            if isinstance(res, Message):
                return res
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                try:
                    res = await event.bot.edit_message_reply_markup(chat_id=chat_id, message_id=pid, reply_markup=kb)
                    if isinstance(res, Message):
                        return res
                except TelegramBadRequest:
                    pass
    return await open_panel(event.bot, chat_id, text, kb)


async def greet(bot: Bot, chat_id: int, text: str) -> None:
    """Короткое приветствие с нижней кнопкой «🏠 Главное меню» (старое приветствие удаляется)."""
    old = GREETINGS.get(chat_id)
    msg = await bot.send_message(chat_id, text, reply_markup=menu_keyboard())
    GREETINGS[chat_id] = msg.message_id
    await _delete(bot, chat_id, old)


# ======================= прогресс долгих операций =======================
class Live:
    """Сообщение, которое обновляется по ходу долгой операции (установка, создание ключа…)."""

    def __init__(self, msg: Message, header: str):
        self.msg, self.header = msg, header
        self.log: list[str] = []
        self.last = 0.0
        self.lost = False   # сообщение удалили (например, открыли меню) — итог пришлём новым

    async def __call__(self, line: str) -> None:
        # шаги одной сессии заменяют друг друга, чтобы сообщение не росло
        if self.log and line.startswith("🚀") and self.log[-1].startswith("🚀"):
            self.log[-1] = line
        else:
            self.log.append(line)
        await self.flush()

    def body(self) -> str:
        log = "\n".join(esc(l) for l in self.log[-10:])
        return f"{self.header}\n\n{log}" if log else self.header

    async def flush(self, force: bool = False, extra: str = "", kb=None) -> None:
        if self.lost or (not force and time.monotonic() - self.last < 1.5):
            return
        self.last = time.monotonic()
        try:
            await self.msg.edit_text((self.body() + extra)[:4090], reply_markup=kb, disable_web_page_preview=True)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                self.lost = True

    async def _final(self, text: str, kb) -> None:
        await self.flush(force=True, extra=text, kb=kb)
        if self.lost:  # панель удалили — присылаем итог отдельным сообщением
            self.msg = await open_panel(self.msg.bot, self.msg.chat.id, (self.header + text)[:4090], kb)

    async def fail(self, error: str, output: str = "", kb=None) -> None:
        out = f"\n\n<b>Вывод сервера:</b>\n<pre>{esc(output.strip()[-1400:])}</pre>" if output.strip() else ""
        await self._final(f"\n\n❌ <b>{esc(error)}</b>{out}", kb)

    async def done(self, text: str, kb=None) -> None:
        await self._final(f"\n\n{text}", kb)
