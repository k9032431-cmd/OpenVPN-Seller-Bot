"""Обработчики команд Telegram."""
from __future__ import annotations

import asyncio
import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, KeyboardButton, Message, ReplyKeyboardMarkup

from . import services, texts
from .config import Config
from .db import DB
from .ip_detect import detect_public_ip
from .openvpn import OpenVPN, OpenVPNError, is_ip, is_valid_host

esc = html.escape


def user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_KEY), KeyboardButton(text=texts.BTN_SUB)],
            [KeyboardButton(text=texts.BTN_HOWTO), KeyboardButton(text=texts.BTN_SUPPORT)],
        ],
        resize_keyboard=True,
    )


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


async def answer_long(message: Message, text: str) -> None:
    chunk = ""
    for line in text.splitlines(True):
        if len(chunk) + len(line) > 3800:
            await message.answer(chunk)
            chunk = ""
        chunk += line
    if chunk:
        await message.answer(chunk)


def describe(c) -> str:
    who = "не привязан"
    if c["tg_id"]:
        who = f"<code>{c['tg_id']}</code>"
        if c["tg_username"]:
            who += f" @{esc(c['tg_username'])}"
    status = "🟢 активен" if c["status"] == "active" else "🔴 отключён"
    lines = [
        f"<b>{c['name']}</b> — {status}",
        f"Клиент: {who}",
        f"Срок: {services.fmt_until(c['expires_at'])}",
        f"Создан: {services.fmt_date(c['created_at'])}",
    ]
    if c["note"]:
        lines.append(f"Заметка: {esc(c['note'])}")
    return "\n".join(lines)


# ======================= АДМИН =======================
def build_admin_router(cfg: Config) -> Router:
    r = Router(name="admin")
    r.message.filter(F.from_user.id.in_(cfg.admin_ids))

    @r.message(Command("help", "admin"))
    async def cmd_help(m: Message):
        await m.answer(texts.ADMIN_HELP, reply_markup=user_keyboard())

    @r.message(Command("new"))
    async def cmd_new(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, bot_username: str):
        args = (command.args or "").split(maxsplit=2)
        if not args or (len(args) > 1 and not args[1].isdigit()):
            return await m.answer("Использование: <code>/new имя [дней] [заметка]</code>")
        name = args[0]
        days = int(args[1]) if len(args) > 1 else 0
        note = args[2] if len(args) > 2 else None
        existing = db.get(name)
        if existing and existing["status"] == "active":
            return await m.answer(f"Ключ <b>{esc(name)}</b> уже существует. /info {esc(name)}")
        try:
            await ovpn.create_client(name)
        except OpenVPNError as e:
            return await m.answer(f"❌ {esc(str(e))}")
        token = db.add_client(name, days, note)
        await m.answer(
            f"✅ Ключ <b>{name}</b> создан, срок: {services.fmt_until(db.get(name)['expires_at'])}\n\n"
            f"Отправьте клиенту эту ссылку:\n{invite_link(bot_username, token)}\n\n"
            "Ссылка одноразовая: она привязывает ключ к первому, кто её открыл."
        )
        await m.answer_document(BufferedInputFile(ovpn.build_ovpn(name), f"{name}.ovpn"))

    @r.message(Command("list"))
    async def cmd_list(m: Message, db: DB):
        rows = db.list_all()
        if not rows:
            return await m.answer("Ключей пока нет. Создайте: /new имя 30")
        active = sum(1 for c in rows if c["status"] == "active")
        lines = [f"<b>Ключей: {len(rows)}, активных: {active}</b>\n"]
        for c in rows:
            icon = "🟢" if c["status"] == "active" else "🔴"
            bound = "👤" if c["tg_id"] else "🔗"
            note = f" — {esc(c['note'])}" if c["note"] else ""
            lines.append(f"{icon}{bound} <code>{c['name']}</code> · "
                         f"{services.fmt_until(c['expires_at'])}{note}")
        lines.append("\n👤 привязан к Telegram · 🔗 ждёт перехода по ссылке")
        await answer_long(m, "\n".join(lines))

    @r.message(Command("info"))
    async def cmd_info(m: Message, command: CommandObject, db: DB, bot_username: str):
        c = db.get((command.args or "").strip())
        if not c:
            return await m.answer("Не найден. Использование: <code>/info имя</code>")
        text = describe(c)
        if c["status"] == "active" and not c["tg_id"]:
            text += f"\n\nСсылка: {invite_link(bot_username, c['invite_token'])}"
        await m.answer(text)

    @r.message(Command("get"))
    async def cmd_get(m: Message, command: CommandObject, ovpn: OpenVPN):
        name = (command.args or "").strip()
        if not ovpn.is_valid_client(name):
            return await m.answer("Активный сертификат не найден. Использование: <code>/get имя</code>")
        await m.answer_document(BufferedInputFile(ovpn.build_ovpn(name), f"{name}.ovpn"))

    @r.message(Command("link"))
    async def cmd_link(m: Message, command: CommandObject, db: DB, bot_username: str):
        name = (command.args or "").strip()
        c = db.get(name)
        if not c or c["status"] != "active":
            return await m.answer("Активный ключ не найден. Использование: <code>/link имя</code>")
        token = db.reset_invite(name)
        await m.answer(f"🔗 Новая ссылка для <b>{name}</b> (старая привязка снята):\n"
                       f"{invite_link(bot_username, token)}")

    @r.message(Command("extend"))
    async def cmd_extend(m: Message, command: CommandObject, db: DB, bot: Bot):
        args = (command.args or "").split()
        if len(args) != 2 or not args[1].isdigit():
            return await m.answer("Использование: <code>/extend имя дней</code> (0 — бессрочно)")
        c = db.get(args[0])
        if not c or c["status"] != "active":
            return await m.answer("Активный ключ не найден. Отключённый ключ продлить нельзя — создайте новый /new.")
        until = services.fmt_until(db.extend(args[0], int(args[1])))
        await m.answer(f"✅ <b>{args[0]}</b>: {until}")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=args[0], until=until))

    @r.message(Command("revoke"))
    async def cmd_revoke(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, bot: Bot):
        name = (command.args or "").strip()
        if not name:
            return await m.answer("Использование: <code>/revoke имя</code>")
        if not db.get(name) and not ovpn.is_valid_client(name):
            return await m.answer("Ключ не найден.")
        try:
            await services.revoke(bot, db, ovpn, name, texts.REVOKED_USER)
        except OpenVPNError as e:
            return await m.answer(f"❌ {esc(str(e))}")
        await m.answer(f"🔴 Ключ <b>{esc(name)}</b> отключён.\n"
                       "Если клиент сейчас подключён, он отвалится при следующем переподключении.")

    @r.message(Command("sync"))
    async def cmd_sync(m: Message, db: DB, ovpn: OpenVPN):
        valid = set(ovpn.valid_clients())
        added = [n for n in sorted(valid) if not db.get(n) or db.get(n)["status"] != "active"]
        for n in added:
            db.add_client(n)
        gone = [c["name"] for c in db.active() if c["name"] not in valid]
        for n in gone:
            db.set_status(n, "revoked")
        text = f"🔄 Добавлено из easy-rsa: {len(added)}"
        if added:
            text += "\n" + ", ".join(f"<code>{n}</code>" for n in added)
            text += "\nСсылки для клиентов: /info имя"
        text += f"\nПомечено отключёнными (отозваны вне бота): {len(gone)}"
        await m.answer(text)

    @r.message(Command("ip"))
    async def cmd_ip(m: Message, ovpn: OpenVPN, cfg: Config):
        host, port = ovpn.get_remote()
        version = 6 if is_ip(host) and ":" in host else 4
        real = await detect_public_ip(version) or "не удалось определить"
        auto = "включено" if cfg.ip_auto_update else "выключено"
        if not is_ip(host):
            auto += " (в ключах домен — IP не отслеживается)"
        await m.answer(f"Адрес в ключах: <code>{host}:{port}</code>\n"
                       f"Реальный IP сервера: <code>{real}</code>\n"
                       f"Автообновление: {auto}, проверка каждые {cfg.ip_check_interval} c")

    @r.message(Command("setip"))
    async def cmd_setip(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, cfg: Config, bot: Bot):
        host = (command.args or "").strip()
        if not is_valid_host(host):
            return await m.answer("Использование: <code>/setip 1.2.3.4</code> или <code>/setip vpn.example.com</code>")
        await m.answer("⏳ Меняю адрес и рассылаю ключи…")
        await services.apply_new_host(bot, db, ovpn, cfg, host, "вручную")

    @r.message(Command("push"))
    async def cmd_push(m: Message, db: DB, ovpn: OpenVPN, bot: Bot):
        await m.answer("⏳ Рассылаю ключи…")
        ok, fail = await services.push_configs(bot, db, ovpn, texts.PUSH_CAPTION)
        await m.answer(f"✅ Отправлено: {ok}, ошибок: {fail}.")

    @r.message(Command("broadcast"))
    async def cmd_broadcast(m: Message, command: CommandObject, db: DB, bot: Bot):
        text = (command.args or "").strip()
        if not text:
            return await m.answer("Использование: <code>/broadcast текст</code>")
        ok = fail = 0
        for uid in db.user_ids():
            if await services.safe_send(bot, uid, f"📢 {esc(text)}"):
                ok += 1
            else:
                fail += 1
            await asyncio.sleep(0.05)
        await m.answer(f"📢 Доставлено: {ok}, ошибок: {fail}.")

    return r


# ======================= КЛИЕНТ =======================
def build_user_router() -> Router:
    r = Router(name="user")

    @r.message(CommandStart())
    async def cmd_start(m: Message, command: CommandObject, db: DB, ovpn: OpenVPN, cfg: Config):
        token = (command.args or "").strip()
        if not token:
            if m.from_user.id in cfg.admin_ids:
                return await m.answer(texts.ADMIN_HELP, reply_markup=user_keyboard())
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
    async def fallback(m: Message):
        await m.answer(texts.WELCOME_BACK, reply_markup=user_keyboard())

    return r
