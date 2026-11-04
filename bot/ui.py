"""Мелочи интерфейса: клавиатуры, редактирование сообщений, «живой» прогресс."""
from __future__ import annotations

import html
import time

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

esc = html.escape


def ikb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row
    ])


async def edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:  # «message is not modified» или старое сообщение
        pass


class Live:
    """Сообщение, которое обновляется по ходу долгой операции (установка, создание ключа…)."""

    def __init__(self, msg: Message, header: str):
        self.msg, self.header = msg, header
        self.log: list[str] = []
        self.last = 0.0

    async def __call__(self, line: str) -> None:
        # шаги одной сессии заменяют друг друга, чтобы сообщение не росло
        if self.log and line.startswith("🚀") and self.log[-1].startswith("🚀"):
            self.log[-1] = line
        else:
            self.log.append(line)
        await self.flush()

    def body(self) -> str:
        return self.header + "\n".join(esc(l) for l in self.log[-12:])

    async def flush(self, force: bool = False, extra: str = "", kb=None) -> None:
        if not force and time.monotonic() - self.last < 1.5:
            return
        self.last = time.monotonic()
        try:
            await self.msg.edit_text((self.body() + extra)[-4090:], reply_markup=kb,
                                     disable_web_page_preview=True)
        except TelegramBadRequest:
            pass

    async def fail(self, error: str, output: str = "", kb=None) -> None:
        out = f"\n\n<b>Вывод:</b>\n<pre>{esc(output.strip()[-1500:])}</pre>" if output.strip() else ""
        await self.flush(force=True, extra=f"\n\n❌ <b>{esc(error)}</b>{out}", kb=kb)

    async def done(self, text: str, kb=None) -> None:
        await self.flush(force=True, extra=f"\n\n{text}", kb=kb)
