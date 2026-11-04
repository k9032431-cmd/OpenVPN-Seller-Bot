"""Обработчики Telegram.

Админ: серверы (добавить, установить скрипт, сменить IP, перенести клиентов), ключи
(создать, продлить, отключить), рассылка. Скрипты — в scripts_ui.py.
Клиент: получает ключ по ссылке-приглашению и пользуется своим меню.
"""
from __future__ import annotations

import asyncio
import html
import math
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from . import engine, services, texts
from .config import Config
from .db import DB
from .engine import ERRORS, NAME_RE, Fleet, is_valid_host
from .remote import RemoteError, bootstrap_ssh
from .scripts_ui import scripts_list
from .ui import Live, edit, ikb

esc = html.escape
PAGE_SIZE = 8
SSH_TARGET_RE = re.compile(r"^(?:(?P<user>[a-z_][a-z0-9_-]*)@)?(?P<host>[^\s:@]+)(?::(?P<port>\d{1,5}))?$")
CANCEL = [("❌ Отмена", "cancel")]
_background: set[asyncio.Task] = set()


def background(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


class NewKey(StatesGroup):
    server = State()
    script = State()
    name = State()
    days = State()


class AddServer(StatesGroup):
    name = State()
    host = State()
    password = State()


class ChangeIP(StatesGroup):
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
        [KeyboardButton(text=texts.BTN_SERVERS), KeyboardButton(text=texts.BTN_SCRIPTS)],
        [KeyboardButton(text=texts.BTN_BCAST), KeyboardButton(text=texts.BTN_HELP)],
    ])


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


def name_of(row) -> str:
    return row["name"] if row else "?"


# ======================= ключи: тексты =======================
def key_card(db: DB, c, bot_username: str):
    who = "ещё не открыл ссылку"
    if c["tg_id"]:
        who = f"<code>{c['tg_id']}</code>" + (f" @{esc(c['tg_username'])}" if c["tg_username"] else "")
    lines = [
        f"🔑 <b>{c['name']}</b> — {'🟢 активен' if c['status'] == 'active' else '🔴 отключён'}",
        f"Сервер: {esc(name_of(db.server(c['server_id'])))} · {esc(name_of(db.script(c['script_id'])))}",
        f"Клиент: {who}",
        f"Срок: {services.fmt_until(c['expires_at'])}",
        f"Создан: {services.fmt_date(c['created_at'])}",
    ]
    if c["note"]:
        lines.append(f"Заметка: {esc(c['note'])}")
    if c["status"] == "active" and not c["tg_id"]:
        lines.append(f"\n🔗 Ссылка для клиента:\n{invite_link(bot_username, c['invite_token'])}")
    i = c["id"]
    if c["status"] != "active":
        return "\n".join(lines), ikb([("⬅️ К списку", "kp:0")])
    return "\n".join(lines), ikb(
        [("📄 Получить ключ", f"kf:{i}"), ("🔗 Новая ссылка", f"kl:{i}")],
        [("+30 дней", f"ke:{i}:30"), ("+90 дней", f"ke:{i}:90"), ("♾", f"ke:{i}:0")],
        [("⛔ Отключить", f"kr:{i}")],
        [("⬅️ К списку", "kp:0")])


def keys_list(db: DB, page: int):
    rows = db.list_all()
    servers = db.servers()
    names = {s["id"]: s["name"][:10] for s in servers}
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    buttons = []
    for c in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = ("🟢" if c["status"] == "active" else "🔴") + ("👤" if c["tg_id"] else "🔗")
        until = "♾" if not c["expires_at"] else services.fmt_date(c["expires_at"])[:10]
        where = f" · {names.get(c['server_id'], '?')}" if len(servers) > 1 else ""
        buttons.append([(f"{icon} {c['name']}{where} · {until}", f"k:{c['id']}")])
    if pages > 1:
        buttons.append([("◀️", f"kp:{page - 1}"), (f"{page + 1}/{pages}", "noop"), ("▶️", f"kp:{page + 1}")])
    buttons.append([(texts.BTN_NEW, "new")])
    active = sum(1 for c in rows if c["status"] == "active")
    text = (f"📋 <b>Ключи: {len(rows)}, активных: {active}</b>\nНажмите на ключ, чтобы управлять им.\n"
            "🟢 активен · 🔴 отключён · 👤 у клиента · 🔗 ссылку не открыли") if rows else \
        "Ключей пока нет. Нажмите «➕ Новый ключ»."
    return text, ikb(*buttons)


# ======================= серверы: тексты =======================
def servers_list(db: DB):
    servers = db.servers()
    rows = [[(f"🖥 {s['name']} · {s['host']} · 🔑{db.count_active(s['id'])}", f"s:{s['id']}")] for s in servers]
    rows.append([("➕ Добавить сервер", "sa")])
    text = ("🖥 <b>Ваши серверы</b>\nНажмите на сервер, чтобы управлять им." if servers else
            "🖥 <b>Серверов пока нет</b>\n\nНажмите «➕ Добавить сервер». Понадобится только IP и пароль root.")
    return text, ikb(*rows)


async def server_card(db: DB, fleet: Fleet, sid: int):
    s = db.server(sid)
    if s is None:
        return "Сервер не найден.", ikb([("⬅️ К серверам", "sl")])
    error = ""
    try:
        await fleet.remote(s).ping()
    except RemoteError as e:
        error = str(e)
    busy = sid in fleet.busy
    count = db.count_active(sid)
    lines = [f"🖥 <b>{esc(s['name'])}</b>", f"SSH: <code>{s['user']}@{s['host']}:{s['port']}</code>",
             "⏳ Сейчас идёт установка или перенос…" if busy else
             (f"⚠️ Нет связи: {esc(error)}" if error else "✅ На связи"), "", "<b>Скрипты:</b>"]
    rows = []
    scripts = db.scripts(only_enabled=True)
    for sc in scripts:
        ver = db.installed_version(sid, sc["id"])
        if ver == sc["version"]:
            lines.append(f"✅ {esc(sc['name'])} — установлен")
        elif ver is None:
            lines.append(f"➖ {esc(sc['name'])} — не установлен (поставится сам при первом ключе)")
            rows.append([(f"⚙️ Установить {sc['name']}", f"si:{sid}:{sc['id']}")])
        else:
            lines.append(f"🔄 {esc(sc['name'])} — скрипт изменился, переустановится при следующем ключе")
            rows.append([(f"🔄 Переустановить {sc['name']}", f"si:{sid}:{sc['id']}")])
    if not scripts:
        lines.append("Пока нет — добавьте в «📜 Скрипты».")
    lines.append(f"\nАктивных ключей: {count}")
    if busy:
        rows = []
    else:
        rows.append([("✏️ Сменить IP", f"sc_ip:{sid}"), ("🔄 Разослать ключи", f"sp:{sid}")])
        if count and len(db.servers()) > 1:
            rows.append([("🔁 Перенести клиентов на другой сервер", f"st:{sid}")])
    rows.append([("♻️ Обновить", f"s:{sid}"), ("🗑 Удалить", f"sd:{sid}")])
    rows.append([("⬅️ К серверам", "sl")])
    return "\n".join(lines), ikb(*rows)


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

    @r.message(F.text == texts.BTN_KEYS)
    async def menu_keys(m: Message, state: FSMContext, db: DB):
        await state.clear()
        text, kb = keys_list(db, 0)
        await m.answer(text, reply_markup=kb)

    @r.message(F.text == texts.BTN_SERVERS)
    async def menu_servers(m: Message, state: FSMContext, db: DB):
        await state.clear()
        text, kb = servers_list(db)
        await m.answer(text, reply_markup=kb)

    @r.message(F.text == texts.BTN_SCRIPTS)
    async def menu_scripts(m: Message, state: FSMContext, db: DB):
        await state.clear()
        text, kb = scripts_list(db)
        await m.answer(text, reply_markup=kb)

    @r.message(F.text == texts.BTN_BCAST)
    async def menu_bcast(m: Message, state: FSMContext):
        await state.set_state(Broadcast.text)
        await m.answer("📢 Напишите сообщение — его получат все клиенты.", reply_markup=ikb(CANCEL))

    @r.callback_query(F.data == "cancel")
    async def cb_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await edit(cb, "Отменено.")

    @r.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()

    # ================= СЕРВЕРЫ =================
    @r.callback_query(F.data == "sl")
    async def cb_servers(cb: CallbackQuery, db: DB):
        await cb.answer()
        await edit(cb, *servers_list(db))

    @r.callback_query(F.data.regexp(r"^s:\d+$"))
    async def cb_server(cb: CallbackQuery, db: DB, fleet: Fleet):
        await cb.answer("Проверяю сервер…")
        await edit(cb, *await server_card(db, fleet, int(cb.data[2:])))

    # ---------- добавить сервер ----------
    @r.callback_query(F.data == "sa")
    async def cb_add(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(AddServer.name)
        await cb.message.answer("🖥 <b>Новый сервер</b>\n\nШаг 1 из 3. Как назвать сервер?\n"
                                "Например: <code>Германия-1</code>", reply_markup=ikb(CANCEL))

    @r.message(AddServer.name, F.text)
    async def add_name(m: Message, state: FSMContext):
        await state.update_data(name=m.text.strip()[:40])
        await state.set_state(AddServer.host)
        await m.answer("Шаг 2 из 3. Напишите <b>IP сервера</b>, например <code>1.2.3.4</code>\n"
                       "Если SSH-порт не 22: <code>1.2.3.4:2222</code>", reply_markup=ikb(CANCEL))

    @r.message(AddServer.host, F.text)
    async def add_host(m: Message, state: FSMContext):
        match = SSH_TARGET_RE.match(m.text.strip())
        if not match or not is_valid_host(match["host"]) or int(match["port"] or 22) > 65535:
            return await m.answer("❗ Не похоже на IP. Пример: <code>1.2.3.4</code>", reply_markup=ikb(CANCEL))
        user = match["user"] or "root"
        await state.update_data(host=match["host"], port=int(match["port"] or 22), user=user)
        await state.set_state(AddServer.password)
        await m.answer(f"Шаг 3 из 3. Напишите <b>пароль {user}</b> от сервера.\n\n"
                       "🔒 Бот зайдёт один раз, добавит свой ключ и удалит ваше сообщение с паролем. "
                       "Пароль нигде не сохраняется.", reply_markup=ikb(CANCEL))

    @r.message(AddServer.password, F.text)
    async def add_password(m: Message, state: FSMContext, db: DB, fleet: Fleet):
        password = m.text
        try:
            await m.delete()
        except TelegramBadRequest:
            pass
        data = await state.get_data()
        status = await m.answer(f"⏳ Подключаюсь к {data['host']}…")
        try:
            host_key = await bootstrap_ssh(data["host"], data["port"], data["user"], password, fleet.pubkey)
        except RemoteError as e:
            return await status.edit_text(f"❌ {esc(str(e))}\n\nНапишите пароль ещё раз:", reply_markup=ikb(CANCEL))
        await state.clear()
        sid = db.add_server(data["name"], "ssh", data["host"], data["port"], data["user"], host_key)
        text, kb = await server_card(db, fleet, sid)
        await status.edit_text(f"✅ Сервер добавлен!\n\n{text}", reply_markup=kb)

    # ---------- установка скрипта ----------
    @r.callback_query(F.data.startswith("si:"))
    async def cb_install(cb: CallbackQuery, db: DB, fleet: Fleet):
        _, sid, scid = cb.data.split(":")
        server, script = db.server(int(sid)), db.script(int(scid))
        if server["id"] in fleet.busy:
            return await cb.answer("На сервере уже идёт операция", show_alert=True)
        await cb.answer()
        live = Live(cb.message, f"⚙️ <b>Установка {esc(script['name'])}</b> · 🖥 {esc(server['name'])}\n"
                                "Обычно 2–5 минут. Можно пользоваться ботом, я обновлю это сообщение.\n\n")

        async def job():
            fleet.busy.add(server["id"])
            try:
                remote = fleet.remote(server)
                async with remote.busy:
                    await engine.install(fleet, db, server, script, live, force=True)
                await live.done(f"🎉 <b>{esc(script['name'])} установлен!</b> Теперь можно создавать ключи: "
                                "«➕ Новый ключ».", ikb([("🖥 К серверу", f"s:{server['id']}")]))
            except ERRORS as e:
                await live.fail(str(e), getattr(e, "output", ""),
                                ikb([("🔄 Попробовать снова", cb.data)], [("🖥 К серверу", f"s:{server['id']}")]))
            finally:
                fleet.busy.discard(server["id"])
        background(job())

    # ---------- рассылка ключей ----------
    @r.callback_query(F.data.startswith("sp:"))
    async def cb_push(cb: CallbackQuery, db: DB, bot: Bot):
        await cb.answer("Рассылаю…")
        ok, fail = await services.push_configs(bot, db, int(cb.data[3:]), texts.PUSH_CAPTION)
        await cb.message.answer(f"✅ Ключи разосланы: {ok}, не доставлено: {fail}.")

    # ---------- смена IP ----------
    @r.callback_query(F.data.startswith("sc_ip:"))
    async def cb_change_ip(cb: CallbackQuery, state: FSMContext, db: DB):
        sid = int(cb.data.split(":")[1])
        await cb.answer()
        await state.set_state(ChangeIP.host)
        await state.update_data(sid=sid)
        await cb.message.answer(
            f"🌐 <b>Смена IP</b> · {esc(name_of(db.server(sid)))}\nСейчас: <code>{db.server(sid)['host']}</code>\n\n"
            "Напишите <b>новый IP</b>. Пароль вводить не нужно.\n\n"
            "Бот подключится по новому IP, выполнит шаги «Смена IP» скрипта, заменит IP во всех ключах "
            "и <b>сам разошлёт их клиентам</b>.", reply_markup=ikb(CANCEL))

    @r.message(ChangeIP.host, F.text)
    async def change_ip_text(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot: Bot):
        host = m.text.strip().lower()
        sid = (await state.get_data())["sid"]
        server = db.server(sid)
        if not is_valid_host(host):
            return await m.answer("❗ Это не похоже на IP. Пример: <code>1.2.3.4</code>", reply_markup=ikb(CANCEL))
        if host == server["host"]:
            return await m.answer("❗ Это текущий IP сервера. Напишите новый:", reply_markup=ikb(CANCEL))
        await state.clear()
        status = await m.answer("⏳")
        live = Live(status, f"🌐 <b>Смена IP</b> · {esc(server['name'])}\n"
                            f"<code>{server['host']}</code> → <code>{esc(host)}</code>\n\n")
        await live(f"⏳ Подключаюсь к {host}…")
        try:
            updated, ok, fail, warnings = await services.change_address(bot, db, fleet, sid, host, live)
        except ERRORS as e:
            return await live.fail(f"{e}\n\nIP не изменён. Проверьте адрес. Если это другой (новый) сервер — "
                                   "добавьте его и перенесите клиентов.", getattr(e, "output", ""))
        warn = "".join(f"\n⚠️ {esc(w)}" for w in warnings)
        await live.done(f"✅ <b>Готово.</b> Ключей обновлено: {updated}. Клиентам отправлено: {ok}, "
                        f"не доставлено: {fail}.{warn}", ikb([("🖥 К серверу", f"s:{sid}")]))

    # ---------- перенос клиентов ----------
    @r.callback_query(F.data.startswith("st:"))
    async def cb_transfer_pick(cb: CallbackQuery, db: DB):
        src = int(cb.data[3:])
        await cb.answer()
        await edit(cb, f"🔁 Куда перенести клиентов с «{esc(name_of(db.server(src)))}»?\n"
                       "Каждый клиент получит новый ключ от выбранного сервера, сроки сохранятся.",
                   ikb(*[[(f"🖥 {s['name']}", f"sT:{src}:{s['id']}")] for s in db.servers() if s["id"] != src],
                       [("⬅️ Назад", f"s:{src}")]))

    @r.callback_query(F.data.startswith("sT:"))
    async def cb_transfer_confirm(cb: CallbackQuery, db: DB):
        _, src, dst = cb.data.split(":")
        await cb.answer()
        await edit(cb, f"Перенести {db.count_active(int(src))} клиентов\n"
                       f"«{esc(name_of(db.server(int(src))))}» → «{esc(name_of(db.server(int(dst))))}»?",
                   ikb([("✅ Да, перенести", f"sY:{src}:{dst}"), ("Нет", f"s:{src}")]))

    @r.callback_query(F.data.startswith("sY:"))
    async def cb_transfer(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        _, src, dst = cb.data.split(":")
        src, dst = int(src), int(dst)
        if {src, dst} & fleet.busy:
            return await cb.answer("Сервер сейчас занят", show_alert=True)
        await cb.answer()
        live = Live(cb.message, f"🔁 <b>Перенос клиентов</b> → {esc(name_of(db.server(dst)))}\n\n")

        async def job():
            fleet.busy.update({src, dst})
            try:
                ok, errors = await services.transfer_clients(bot, db, fleet, src, dst, live)
                err = "".join(f"\n❌ {esc(x[:150])}" for x in errors[:10])
                await live.done(f"✅ Перенесено: {ok}. Клиенты получили новые ключи.{err}",
                                ikb([("🖥 К серверу", f"s:{dst}")]))
            except Exception as e:
                await live.fail(f"Перенос не удался: {e}")
            finally:
                fleet.busy.difference_update({src, dst})
        background(job())

    # ---------- удаление ----------
    @r.callback_query(F.data.startswith("sd:"))
    async def cb_delete_ask(cb: CallbackQuery, db: DB):
        sid = int(cb.data[3:])
        await cb.answer()
        n = db.count_active(sid)
        warn = f"\n{n} активных ключей будут отключены в боте, клиенты получат уведомление." if n else ""
        await edit(cb, f"🗑 Удалить сервер «{esc(name_of(db.server(sid)))}» из бота?{warn}\n"
                       "На самом сервере ничего не удаляется.",
                   ikb([("Да, удалить", f"sD:{sid}"), ("Нет", f"s:{sid}")]))

    @r.callback_query(F.data.startswith("sD:"))
    async def cb_delete(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        await services.delete_server(bot, db, fleet, int(cb.data[3:]))
        await cb.answer("Сервер удалён")
        await edit(cb, *servers_list(db))

    # ================= НОВЫЙ КЛЮЧ =================
    @r.message(F.text == texts.BTN_NEW)
    async def menu_new(m: Message, state: FSMContext, db: DB):
        await state.clear()
        servers, scripts = db.servers(), db.scripts(only_enabled=True)
        if not scripts:
            return await m.answer("Сначала добавьте скрипт: «📜 Скрипты» → «➕ Добавить скрипт» "
                                  "(или «⚡ Готовый: OpenVPN»).", reply_markup=admin_keyboard())
        if not servers:
            return await m.answer("Сначала добавьте сервер: «🖥 Серверы» → «➕ Добавить сервер».",
                                  reply_markup=admin_keyboard())
        if len(servers) == 1:
            await state.update_data(sid=servers[0]["id"])
            return await ask_script(m, state, db)
        await state.set_state(NewKey.server)
        await m.answer("🖥 На каком сервере создать ключ?",
                       reply_markup=ikb(*[[(f"🖥 {s['name']}", f"ns:{s['id']}")] for s in servers], CANCEL))

    async def ask_script(m: Message, state: FSMContext, db: DB):
        scripts = db.scripts(only_enabled=True)
        if len(scripts) == 1:
            await state.update_data(scid=scripts[0]["id"])
            return await ask_name(m, state)
        await state.set_state(NewKey.script)
        await m.answer("📜 Какой VPN?",
                       reply_markup=ikb(*[[(f"📜 {s['name']}", f"nsc:{s['id']}")] for s in scripts], CANCEL))

    async def ask_name(m: Message, state: FSMContext):
        await state.set_state(NewKey.name)
        await m.answer("✏️ Напишите <b>название ключа</b> латиницей, например <code>ahmet</code>\n"
                       "Можно добавить заметку через пробел: <code>ahmet Ахмет +99361234567</code>",
                       reply_markup=ikb(CANCEL))

    @r.callback_query(F.data == "new")
    async def cb_new(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await menu_new(cb.message, state, db)

    @r.callback_query(NewKey.server, F.data.startswith("ns:"))
    async def new_server(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await state.update_data(sid=int(cb.data[3:]))
        await edit(cb, f"🖥 Сервер: {esc(name_of(db.server(int(cb.data[3:]))))}")
        await ask_script(cb.message, state, db)

    @r.callback_query(NewKey.script, F.data.startswith("nsc:"))
    async def new_script(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await state.update_data(scid=int(cb.data[4:]))
        await edit(cb, f"📜 VPN: {esc(name_of(db.script(int(cb.data[4:]))))}")
        await ask_name(cb.message, state)

    @r.message(NewKey.name, F.text)
    async def new_name(m: Message, state: FSMContext, db: DB):
        parts = m.text.strip().split(maxsplit=1)
        name, note = parts[0], (parts[1] if len(parts) > 1 else None)
        if not NAME_RE.match(name):
            return await m.answer("❗ Только латинские буквы, цифры, «_» и «-», без пробелов "
                                  "(до 32 символов). Попробуйте ещё раз:", reply_markup=ikb(CANCEL))
        old = db.get_by_name((await state.get_data())["sid"], name)
        if old and old["status"] == "active":
            return await m.answer(f"❗ Ключ <b>{name}</b> уже есть. Придумайте другое название:",
                                  reply_markup=ikb(CANCEL))
        await state.update_data(name=name, note=note)
        await state.set_state(NewKey.days)
        await m.answer(f"⏳ На какой срок ключ <b>{name}</b>?\nНажмите кнопку или напишите число дней.",
                       reply_markup=ikb([("7 дней", "nd:7"), ("30 дней", "nd:30")],
                                        [("90 дней", "nd:90"), ("♾ Бессрочно", "nd:0")], CANCEL))

    async def create_key(m: Message, state: FSMContext, days: int, db: DB, fleet: Fleet, bot_username: str):
        data = await state.get_data()
        await state.clear()
        server, script, name = db.server(data["sid"]), db.script(data["scid"]), data["name"]
        status = await m.answer("⏳")
        live = Live(status, f"🔑 <b>Создание ключа</b> <code>{name}</code>\n"
                            f"📜 {esc(script['name'])} · 🖥 {esc(server['name'])}\n\n")
        await live("⏳ Подключение к серверу…")
        fleet.busy.add(server["id"])
        try:
            value, filename = await engine.create_key(fleet, db, server, script, name, live)
        except ERRORS as e:
            return await live.fail(str(e), getattr(e, "output", ""))
        finally:
            fleet.busy.discard(server["id"])
        cid = db.add_client(server["id"], script["id"], name, value, filename, days, data.get("note"))
        c = db.get(cid)
        await status.edit_text(
            f"✅ Ключ <b>{name}</b> создан · {services.fmt_until(c['expires_at'])}\n\n"
            f"👉 <b>Перешлите клиенту эту ссылку:</b>\n{invite_link(bot_username, c['invite_token'])}\n\n"
            "Клиент откроет её, нажмёт «Запустить» — и получит ключ.\n"
            "Сам ключ ниже — если хотите отправить его сами.", disable_web_page_preview=True)
        await services.send_key(m.bot, m.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(NewKey.days, F.data.startswith("nd:"))
    async def new_days_cb(cb: CallbackQuery, state: FSMContext, db: DB, fleet: Fleet, bot_username: str):
        await cb.answer()
        await edit(cb, f"Срок: {'бессрочно' if cb.data == 'nd:0' else cb.data[3:] + ' дней'}")
        await create_key(cb.message, state, int(cb.data[3:]), db, fleet, bot_username)

    @r.message(NewKey.days, F.text.regexp(r"^\d{1,4}$"))
    async def new_days_text(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot_username: str):
        await create_key(m, state, int(m.text), db, fleet, bot_username)

    # ================= СПИСОК И КАРТОЧКА КЛЮЧА =================
    @r.callback_query(F.data.startswith("kp:"))
    async def cb_page(cb: CallbackQuery, db: DB):
        await cb.answer()
        await edit(cb, *keys_list(db, int(cb.data[3:])))

    async def show_key(cb: CallbackQuery, db: DB, cid: int, bot_username: str):
        c = db.get(cid)
        if not c:
            return await edit(cb, "Ключ не найден.", ikb([("⬅️ К списку", "kp:0")]))
        await edit(cb, *key_card(db, c, bot_username))

    @r.callback_query(F.data.startswith("k:"))
    async def cb_key(cb: CallbackQuery, db: DB, bot_username: str):
        await cb.answer()
        await show_key(cb, db, int(cb.data[2:]), bot_username)

    @r.callback_query(F.data.startswith("kf:"))
    async def cb_file(cb: CallbackQuery, db: DB, bot: Bot):
        await cb.answer()
        await services.send_key(bot, cb.message.chat.id, db.get(int(cb.data[3:])), texts.KEY_CAPTION)

    @r.callback_query(F.data.startswith("kl:"))
    async def cb_link(cb: CallbackQuery, db: DB, bot_username: str):
        db.reset_invite(int(cb.data[3:]))
        await cb.answer("Новая ссылка готова. Старая больше не работает.", show_alert=True)
        await show_key(cb, db, int(cb.data[3:]), bot_username)

    @r.callback_query(F.data.startswith("ke:"))
    async def cb_extend(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str):
        cid, days = map(int, cb.data[3:].split(":"))
        c = db.get(cid)
        if not c or c["status"] != "active":
            return await cb.answer("Ключ не активен", show_alert=True)
        until = services.fmt_until(db.extend(cid, days))
        await cb.answer(f"Продлено: {until}")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=c["name"], until=until))
        await show_key(cb, db, cid, bot_username)

    @r.callback_query(F.data.startswith("kr:"))
    async def cb_revoke_ask(cb: CallbackQuery, db: DB):
        c = db.get(int(cb.data[3:]))
        await cb.answer()
        await edit(cb, f"⛔ Отключить ключ <b>{c['name']}</b>?\n"
                       "Бот выполнит «Удаление ключа» на сервере. Клиент больше не сможет подключиться.",
                   ikb([("Да, отключить", f"kR:{c['id']}"), ("Нет", f"k:{c['id']}")]))

    @r.callback_query(F.data.startswith("kR:"))
    async def cb_revoke(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot, bot_username: str):
        cid = int(cb.data[3:])
        await cb.answer("Отключаю…")
        try:
            await services.revoke(bot, db, fleet, cid, texts.REVOKED_USER)
        except ERRORS as e:
            return await edit(cb, f"❌ Не удалось отключить на сервере: {esc(str(e))}",
                              ikb([("🔄 Ещё раз", f"kR:{cid}")], [("⬅️ К ключу", f"k:{cid}")]))
        await show_key(cb, db, cid, bot_username)

    # ================= РАССЫЛКА =================
    @r.message(Broadcast.text, F.text)
    async def bcast_text(m: Message, state: FSMContext, db: DB, bot: Bot):
        await state.clear()
        ok = fail = 0
        for uid in db.user_ids():
            if await services.safe_send(bot, uid, f"📢 {esc(m.text)}"):
                ok += 1
            else:
                fail += 1
            await asyncio.sleep(0.05)
        await m.answer(f"📢 Доставлено: {ok}, не доставлено: {fail}.", reply_markup=admin_keyboard())

    # любое другое сообщение админа (кроме /start) — показать меню
    @r.message(F.text, ~F.text.startswith("/"))
    async def admin_fallback(m: Message):
        await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())

    return r


# ======================= КЛИЕНТ =======================
def build_user_router() -> Router:
    r = Router(name="user")

    @r.message(CommandStart())
    async def cmd_start(m: Message, command: CommandObject, state: FSMContext, db: DB, cfg: Config):
        await state.clear()
        token = (command.args or "").strip()
        if not token:
            if m.from_user.id in cfg.admin_ids:
                return await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())
            has = db.clients_of(m.from_user.id)
            return await m.answer(texts.WELCOME_BACK if has else texts.NO_KEYS, reply_markup=user_keyboard())
        c = db.get_by_token(token)
        if not c:
            return await m.answer(texts.INVITE_INVALID)
        if c["status"] != "active":
            return await m.answer(texts.INVITE_REVOKED)
        if c["tg_id"] and c["tg_id"] != m.from_user.id:
            return await m.answer(texts.INVITE_TAKEN)
        db.bind(c["id"], m.from_user.id, m.from_user.username)
        await m.answer(texts.WELCOME_BOUND.format(name=c["name"]), reply_markup=user_keyboard())
        await services.send_key(m.bot, m.chat.id, c, texts.KEY_CAPTION)

    @r.message(Command("key"))
    @r.message(F.text == texts.BTN_KEY)
    async def btn_key(m: Message, db: DB):
        keys = db.clients_of(m.from_user.id)
        if not keys:
            return await m.answer(texts.NO_KEYS)
        for c in keys:
            await services.send_key(m.bot, m.chat.id, c, texts.KEY_CAPTION)

    @r.message(F.text == texts.BTN_SUB)
    async def btn_sub(m: Message, db: DB):
        keys = db.clients_of(m.from_user.id)
        if not keys:
            return await m.answer(texts.NO_KEYS)
        await m.answer("\n".join(
            texts.SUB_LINE.format(name=c["name"], until=services.fmt_until(c["expires_at"])) for c in keys))

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
