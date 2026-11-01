"""Обработчики Telegram.

Админ управляет всем кнопками (команды /new, /list и т.д. тоже работают).
Клиент получает ключ по ссылке-приглашению и пользуется своим меню.
"""
from __future__ import annotations

import asyncio
import html
import math

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)

from . import services, texts
from .config import Config
from .db import DB
from .ip_detect import detect_public_ip
from .openvpn import NAME_RE, OpenVPN, OpenVPNError, is_ip, is_valid_host

esc = html.escape
PAGE_SIZE = 8


class NewKey(StatesGroup):
    name = State()
    days = State()


class SetHost(StatesGroup):
    host = State()


class Broadcast(StatesGroup):
    text = State()


# ======================= клавиатуры =======================
def user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=texts.BTN_KEY), KeyboardButton(text=texts.BTN_SUB)],
        [KeyboardButton(text=texts.BTN_HOWTO), KeyboardButton(text=texts.BTN_SUPPORT)],
    ])


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=texts.BTN_NEW), KeyboardButton(text=texts.BTN_KEYS)],
        [KeyboardButton(text=texts.BTN_IP), KeyboardButton(text=texts.BTN_BCAST)],
        [KeyboardButton(text=texts.BTN_HELP)],
    ])


def ikb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
    ])


CANCEL = [("❌ Отмена", "cancel")]


def days_keyboard() -> InlineKeyboardMarkup:
    return ikb([("7 дней", "nd:7"), ("30 дней", "nd:30")],
               [("90 дней", "nd:90"), ("♾ Бессрочно", "nd:0")], CANCEL)


def keys_keyboard(rows, page: int) -> InlineKeyboardMarkup:
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    buttons = []
    for c in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = ("🟢" if c["status"] == "active" else "🔴") + ("👤" if c["tg_id"] else "🔗")
        until = "♾" if not c["expires_at"] else services.fmt_date(c["expires_at"])[:10]
        buttons.append([(f"{icon} {c['name']} · {until}", f"k:{c['name']}")])
    if pages > 1:
        buttons.append([("◀️", f"kp:{page - 1}"), (f"{page + 1}/{pages}", "noop"),
                        ("▶️", f"kp:{page + 1}")])
    buttons.append([(texts.BTN_NEW, "new")])
    return ikb(*buttons)


def key_card_keyboard(c) -> InlineKeyboardMarkup:
    n = c["name"]
    if c["status"] != "active":
        return ikb([("⬅️ К списку", "kp:0")])
    return ikb(
        [("📄 Файл .ovpn", f"kf:{n}"), ("🔗 Новая ссылка", f"kl:{n}")],
        [("+30 дней", f"ke:{n}:30"), ("+90 дней", f"ke:{n}:90"), ("♾", f"ke:{n}:0")],
        [("⛔ Отключить", f"kr:{n}")],
        [("⬅️ К списку", "kp:0")],
    )


# ======================= тексты карточек =======================
def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


def describe(c) -> str:
    who = "ещё не открыл ссылку"
    if c["tg_id"]:
        who = f"<code>{c['tg_id']}</code>"
        if c["tg_username"]:
            who += f" @{esc(c['tg_username'])}"
    status = "🟢 активен" if c["status"] == "active" else "🔴 отключён"
    lines = [
        f"🔑 <b>{c['name']}</b> — {status}",
        f"Клиент: {who}",
        f"Срок: {services.fmt_until(c['expires_at'])}",
        f"Создан: {services.fmt_date(c['created_at'])}",
    ]
    if c["note"]:
        lines.append(f"Заметка: {esc(c['note'])}")
    return "\n".join(lines)


def key_card_text(c, bot_username: str) -> str:
    text = describe(c)
    if c["status"] == "active" and not c["tg_id"]:
        text += f"\n\n🔗 Ссылка для клиента:\n{invite_link(bot_username, c['invite_token'])}"
    return text


def keys_header(rows) -> str:
    active = sum(1 for c in rows if c["status"] == "active")
    return (f"📋 <b>Ключи: {len(rows)}, активных: {active}</b>\n"
            "Нажмите на ключ, чтобы управлять им.\n"
            "🟢 активен · 🔴 отключён · 👤 у клиента · 🔗 ссылку не открыли")


async def ip_text(ovpn: OpenVPN, cfg: Config) -> str:
    host, port = ovpn.get_remote()
    version = 6 if ":" in host else 4
    real = await detect_public_ip(version) or "не удалось определить"
    if not is_ip(host):
        auto = "не нужно — в ключах домен"
    elif cfg.ip_auto_update:
        auto = f"✅ включено (проверка каждые {cfg.ip_check_interval} c)"
    else:
        auto = "выключено"
    return (f"🌐 <b>Адрес сервера</b>\n\n"
            f"В ключах клиентов: <code>{host}:{port}</code>\n"
            f"Реальный IP сейчас: <code>{real}</code>\n"
            f"Автообновление: {auto}\n\n"
            "Если IP поменяется, бот сам разошлёт клиентам новые ключи.")


async def create_key(m: Message, db: DB, ovpn: OpenVPN, bot_username: str,
                     name: str, days: int, note: str | None) -> None:
    try:
        await ovpn.create_client(name)
    except OpenVPNError as e:
        await m.answer(f"❌ {esc(str(e))}", reply_markup=admin_keyboard())
        return
    token = db.add_client(name, days, note)
    c = db.get(name)
    await m.answer(
        f"✅ Ключ <b>{name}</b> создан · {services.fmt_until(c['expires_at'])}\n\n"
        f"👉 <b>Перешлите клиенту эту ссылку:</b>\n{invite_link(bot_username, token)}\n\n"
        "Клиент откроет её, нажмёт «Запустить» — и получит ключ.\n"
        "Файл ниже — на случай, если хотите отправить его сами.",
        reply_markup=admin_keyboard(),
    )
    await m.answer_document(BufferedInputFile(ovpn.build_ovpn(name), f"{name}.ovpn"))


def parse_name(text: str) -> tuple[str, str | None]:
    parts = text.strip().split(maxsplit=1)
    return parts[0], (parts[1] if len(parts) > 1 else None)


# ======================= АДМИН =======================
def build_admin_router(cfg: Config) -> Router:
    r = Router(name="admin")
    is_admin = F.from_user.id.in_(cfg.admin_ids)
    r.message.filter(is_admin)
    r.callback_query.filter(is_admin)

    # ---------- главное меню (работает из любого шага) ----------
    @r.message(F.text == texts.BTN_HELP)
    @r.message(Command("help"))
    async def menu_help(m: Message, state: FSMContext):
        await state.clear()
        await m.answer(texts.HOW_IT_WORKS.format(warn=cfg.warn_days), reply_markup=admin_keyboard())
        await m.answer(texts.ADMIN_HELP)

    @r.message(F.text == texts.BTN_NEW)
    async def menu_new(m: Message, state: FSMContext):
        await state.set_state(NewKey.name)
        await m.answer("✏️ Напишите <b>имя ключа</b> латиницей, например <code>ahmet</code>\n"
                       "Можно добавить заметку через пробел: <code>ahmet Ахмет +99361234567</code>",
                       reply_markup=ikb(CANCEL))

    @r.message(F.text == texts.BTN_KEYS)
    @r.message(Command("list"))
    async def menu_keys(m: Message, state: FSMContext, db: DB, ovpn: OpenVPN):
        await state.clear()
        services.sync_from_easyrsa(db, ovpn)
        rows = db.list_all()
        if not rows:
            return await m.answer("Ключей пока нет. Нажмите «➕ Новый ключ».",
                                  reply_markup=admin_keyboard())
        await m.answer(keys_header(rows), reply_markup=keys_keyboard(rows, 0))

    @r.message(F.text == texts.BTN_IP)
    @r.message(Command("ip"))
    async def menu_ip(m: Message, state: FSMContext, ovpn: OpenVPN):
        await state.clear()
        await m.answer(await ip_text(ovpn, cfg), reply_markup=ikb(
            [("🔄 Разослать ключи всем", "push")], [("✏️ Сменить адрес вручную", "sethost")]))

    @r.message(F.text == texts.BTN_BCAST)
    async def menu_bcast(m: Message, state: FSMContext):
        await state.set_state(Broadcast.text)
        await m.answer("📢 Напишите сообщение — его получат все клиенты.", reply_markup=ikb(CANCEL))

    @r.callback_query(F.data == "cancel")
    async def cb_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text("Отменено.")
        await cb.answer()

    @r.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()

    # ---------- новый ключ ----------
    @r.callback_query(F.data == "new")
    async def cb_new(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await menu_new(cb.message, state)

    @r.message(NewKey.name, F.text)
    async def new_name(m: Message, state: FSMContext, db: DB):
        name, note = parse_name(m.text)
        if not NAME_RE.match(name):
            return await m.answer("❗ Только латинские буквы, цифры, «_» и «-», без пробелов "
                                  "(до 32 символов). Попробуйте ещё раз:", reply_markup=ikb(CANCEL))
        old = db.get(name)
        if old and old["status"] == "active":
            return await m.answer(f"❗ Ключ <b>{name}</b> уже есть. Придумайте другое имя:",
                                  reply_markup=ikb(CANCEL))
        await state.update_data(name=name, note=note)
        await state.set_state(NewKey.days)
        await m.answer(f"⏳ На какой срок ключ <b>{name}</b>?\n"
                       "Нажмите кнопку или напишите число дней.", reply_markup=days_keyboard())

    async def finish_new(m: Message, state: FSMContext, days: int, db, ovpn, bot_username):
        data = await state.get_data()
        await state.clear()
        await create_key(m, db, ovpn, bot_username, data["name"], days, data.get("note"))

    @r.callback_query(NewKey.days, F.data.startswith("nd:"))
    async def new_days_cb(cb: CallbackQuery, state: FSMContext, db: DB, ovpn: OpenVPN, bot_username: str):
        await cb.answer()
        await cb.message.edit_reply_markup(reply_markup=None)
        await finish_new(cb.message, state, int(cb.data[3:]), db, ovpn, bot_username)

    @r.message(NewKey.days, F.text.regexp(r"^\d{1,4}$"))
    async def new_days_text(m: Message, state: FSMContext, db: DB, ovpn: OpenVPN, bot_username: str):
        await finish_new(m, state, int(m.text), db, ovpn, bot_username)

    @r.message(NewKey.days)
    async def new_days_bad(m: Message):
        await m.answer("Нажмите кнопку или напишите число дней (0 — бессрочно).",
                       reply_markup=days_keyboard())

    # ---------- список и карточка ключа ----------
    @r.callback_query(F.data.startswith("kp:"))
    async def cb_page(cb: CallbackQuery, db: DB):
        rows = db.list_all()
        await cb.answer()
        if not rows:
            return await cb.message.edit_text("Ключей пока нет.")
        await cb.message.edit_text(keys_header(rows), reply_markup=keys_keyboard(rows, int(cb.data[3:])))

    async def show_card(cb: CallbackQuery, db: DB, name: str, bot_username: str):
        c = db.get(name)
        if not c:
            return await cb.answer("Ключ не найден", show_alert=True)
        try:
            await cb.message.edit_text(key_card_text(c, bot_username), reply_markup=key_card_keyboard(c),
                                       disable_web_page_preview=True)
        except TelegramBadRequest:  # «message is not modified» — ничего не поменялось
            pass

    @r.callback_query(F.data.startswith("k:"))
    async def cb_card(cb: CallbackQuery, db: DB, bot_username: str):
        await cb.answer()
        await show_card(cb, db, cb.data[2:], bot_username)

    @r.callback_query(F.data.startswith("kf:"))
    async def cb_file(cb: CallbackQuery, ovpn: OpenVPN):
        name = cb.data[3:]
        try:
            await cb.message.answer_document(BufferedInputFile(ovpn.build_ovpn(name), f"{name}.ovpn"))
            await cb.answer()
        except OpenVPNError as e:
            await cb.answer(str(e), show_alert=True)

    @r.callback_query(F.data.startswith("kl:"))
    async def cb_link(cb: CallbackQuery, db: DB, bot_username: str):
        db.reset_invite(cb.data[3:])
        await cb.answer("Новая ссылка готова. Старая больше не работает.", show_alert=True)
        await show_card(cb, db, cb.data[3:], bot_username)

    @r.callback_query(F.data.startswith("ke:"))
    async def cb_extend(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str):
        name, days = cb.data[3:].rsplit(":", 1)
        c = db.get(name)
        if not c or c["status"] != "active":
            return await cb.answer("Ключ не активен", show_alert=True)
        until = services.fmt_until(db.extend(name, int(days)))
        await cb.answer(f"Продлено: {until}")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=name, until=until))
        await show_card(cb, db, name, bot_username)

    @r.callback_query(F.data.startswith("kr:"))
    async def cb_revoke_ask(cb: CallbackQuery):
        name = cb.data[3:]
        await cb.answer()
        await cb.message.edit_text(
            f"⛔ Отключить ключ <b>{name}</b>?\nКлиент больше не сможет подключиться. Отменить нельзя.",
            reply_markup=ikb([("Да, отключить", f"krY:{name}"), ("Нет", f"k:{name}")]))

    @r.callback_query(F.data.startswith("krY:"))
    async def cb_revoke(cb: CallbackQuery, db: DB, ovpn: OpenVPN, bot: Bot, bot_username: str):
        name = cb.data[4:]
        try:
            await services.revoke(bot, db, ovpn, name, texts.REVOKED_USER)
        except OpenVPNError as e:
            return await cb.answer(str(e)[:190], show_alert=True)
        await cb.answer("Ключ отключён")
        await show_card(cb, db, name, bot_username)

    # ---------- IP ----------
    @r.callback_query(F.data == "push")
    @r.message(Command("push"))
    async def do_push(event, db: DB, ovpn: OpenVPN, bot: Bot):
        m = event.message if isinstance(event, CallbackQuery) else event
        if isinstance(event, CallbackQuery):
            await event.answer()
        await m.answer("⏳ Рассылаю ключи…")
        ok, fail = await services.push_configs(bot, db, ovpn, texts.PUSH_CAPTION)
        await m.answer(f"✅ Отправлено: {ok}, не доставлено: {fail}.")

    @r.callback_query(F.data == "sethost")
    async def cb_sethost(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(SetHost.host)
        await cb.message.answer("✏️ Напишите новый IP или домен сервера, например <code>1.2.3.4</code>\n"
                                "Всем клиентам сразу уйдут новые ключи.", reply_markup=ikb(CANCEL))

    async def set_host(m: Message, host: str, db, ovpn, bot):
        if not is_valid_host(host):
            return await m.answer("❗ Это не похоже на IP или домен. Пример: <code>1.2.3.4</code>")
        await m.answer("⏳ Меняю адрес и рассылаю ключи…")
        await services.apply_new_host(bot, db, ovpn, cfg, host, "вручную")

    @r.message(SetHost.host, F.text)
    async def sethost_text(m: Message, state: FSMContext, db: DB, ovpn: OpenVPN, bot: Bot):
        if is_valid_host(m.text.strip()):
            await state.clear()
        await set_host(m, m.text.strip(), db, ovpn, bot)

    @r.message(Command("setip"))
    async def cmd_setip(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, bot: Bot):
        await set_host(m, (command.args or "").strip(), db, ovpn, bot)

    # ---------- рассылка ----------
    async def broadcast(m: Message, text: str, db: DB, bot: Bot):
        ok = fail = 0
        for uid in db.user_ids():
            if await services.safe_send(bot, uid, f"📢 {esc(text)}"):
                ok += 1
            else:
                fail += 1
            await asyncio.sleep(0.05)
        await m.answer(f"📢 Доставлено: {ok}, не доставлено: {fail}.", reply_markup=admin_keyboard())

    @r.message(Broadcast.text, F.text)
    async def bcast_text(m: Message, state: FSMContext, db: DB, bot: Bot):
        await state.clear()
        await broadcast(m, m.text, db, bot)

    @r.message(Command("broadcast"))
    async def cmd_broadcast(m: Message, command: CommandObject, db: DB, bot: Bot):
        if not command.args:
            return await m.answer("Использование: <code>/broadcast текст</code>")
        await broadcast(m, command.args, db, bot)

    # ---------- команды (для тех, кто любит текстом) ----------
    @r.message(Command("new"))
    async def cmd_new(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, bot_username: str):
        args = (command.args or "").split(maxsplit=2)
        if not args or (len(args) > 1 and not args[1].isdigit()):
            return await m.answer("Использование: <code>/new имя [дней] [заметка]</code>")
        days = int(args[1]) if len(args) > 1 else 0
        await create_key(m, db, ovpn, bot_username, args[0], days, args[2] if len(args) > 2 else None)

    @r.message(Command("info"))
    async def cmd_info(m: Message, command: CommandObject, db: DB, bot_username: str):
        c = db.get((command.args or "").strip())
        if not c:
            return await m.answer("Не найден. Использование: <code>/info имя</code>")
        await m.answer(key_card_text(c, bot_username), reply_markup=key_card_keyboard(c))

    @r.message(Command("get"))
    async def cmd_get(m: Message, command: CommandObject, ovpn: OpenVPN):
        name = (command.args or "").strip()
        if not ovpn.is_valid_client(name):
            return await m.answer("Активный ключ не найден. Использование: <code>/get имя</code>")
        await m.answer_document(BufferedInputFile(ovpn.build_ovpn(name), f"{name}.ovpn"))

    @r.message(Command("link"))
    async def cmd_link(m: Message, command: CommandObject, db: DB, bot_username: str):
        name = (command.args or "").strip()
        c = db.get(name)
        if not c or c["status"] != "active":
            return await m.answer("Активный ключ не найден. Использование: <code>/link имя</code>")
        token = db.reset_invite(name)
        await m.answer(f"🔗 Новая ссылка для <b>{name}</b>:\n{invite_link(bot_username, token)}")

    @r.message(Command("extend"))
    async def cmd_extend(m: Message, command: CommandObject, db: DB, bot: Bot):
        args = (command.args or "").split()
        if len(args) != 2 or not args[1].isdigit():
            return await m.answer("Использование: <code>/extend имя дней</code> (0 — бессрочно)")
        c = db.get(args[0])
        if not c or c["status"] != "active":
            return await m.answer("Активный ключ не найден.")
        until = services.fmt_until(db.extend(args[0], int(args[1])))
        await m.answer(f"✅ <b>{args[0]}</b>: {until}")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=args[0], until=until))

    @r.message(Command("revoke"))
    async def cmd_revoke(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, bot: Bot):
        name = (command.args or "").strip()
        if not db.get(name) and not ovpn.is_valid_client(name):
            return await m.answer("Ключ не найден. Использование: <code>/revoke имя</code>")
        try:
            await services.revoke(bot, db, ovpn, name, texts.REVOKED_USER)
        except OpenVPNError as e:
            return await m.answer(f"❌ {esc(str(e))}")
        await m.answer(f"🔴 Ключ <b>{esc(name)}</b> отключён.")

    @r.message(Command("sync"))
    async def cmd_sync(m: Message, db: DB, ovpn: OpenVPN):
        added, gone = services.sync_from_easyrsa(db, ovpn)
        await m.answer(f"🔄 Добавлено из easy-rsa: {len(added)} {', '.join(added)}\n"
                       f"Помечено отключёнными: {len(gone)}")

    # любое другое сообщение админа (кроме /start) — показать меню
    @r.message(F.text, ~F.text.startswith("/"))
    async def admin_fallback(m: Message):
        await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())

    return r


# ======================= КЛИЕНТ =======================
def build_user_router() -> Router:
    r = Router(name="user")

    @r.message(CommandStart())
    async def cmd_start(m: Message, command: CommandObject, state: FSMContext,
                        db: DB, ovpn: OpenVPN, cfg: Config):
        await state.clear()
        token = (command.args or "").strip()
        if not token:
            if m.from_user.id in cfg.admin_ids:
                return await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())
            has = db.clients_of(m.from_user.id)
            return await m.answer(texts.WELCOME_BACK if has else texts.NO_KEYS,
                                  reply_markup=user_keyboard())

        c = db.get_by_token(token)
        if not c:
            return await m.answer(texts.INVITE_INVALID)
        if c["status"] != "active":
            return await m.answer(texts.INVITE_REVOKED)
        if c["tg_id"] and c["tg_id"] != m.from_user.id:
            return await m.answer(texts.INVITE_TAKEN)
        db.bind(c["name"], m.from_user.id, m.from_user.username)
        await m.answer(texts.WELCOME_BOUND.format(name=c["name"]), reply_markup=user_keyboard())
        await services.send_config(m.bot, ovpn, m.chat.id, c["name"], texts.KEY_CAPTION)

    @r.message(Command("key"))
    @r.message(F.text == texts.BTN_KEY)
    async def btn_key(m: Message, db: DB, ovpn: OpenVPN):
        keys = db.clients_of(m.from_user.id)
        if not keys:
            return await m.answer(texts.NO_KEYS)
        for c in keys:
            await services.send_config(m.bot, ovpn, m.chat.id, c["name"], texts.KEY_CAPTION)

    @r.message(F.text == texts.BTN_SUB)
    async def btn_sub(m: Message, db: DB):
        keys = db.clients_of(m.from_user.id)
        if not keys:
            return await m.answer(texts.NO_KEYS)
        await m.answer("\n".join(
            texts.SUB_LINE.format(name=c["name"], until=services.fmt_until(c["expires_at"]))
            for c in keys))

    @r.message(F.text == texts.BTN_HOWTO)
    async def btn_howto(m: Message):
        await m.answer(texts.HOWTO, disable_web_page_preview=True)

    @r.message(F.text == texts.BTN_SUPPORT)
    async def btn_support(m: Message, cfg: Config):
        await m.answer(texts.SUPPORT.format(contact=esc(cfg.support_contact))
                       if cfg.support_contact else texts.SUPPORT_NONE)

    @r.message()
    async def fallback(m: Message, db: DB):
        has = db.clients_of(m.from_user.id)
        await m.answer(texts.WELCOME_BACK if has else texts.NO_KEYS, reply_markup=user_keyboard())

    return r
