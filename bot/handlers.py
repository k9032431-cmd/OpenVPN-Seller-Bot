"""Обработчики Telegram.

Роли:
  • главный админ (ADMIN_IDS) — серверы, скрипты, диллеры, рассылка и все ключи;
  • диллер — только создание ключей и управление СВОИМИ ключами;
  • клиент — получает ключ по ссылке-приглашению и пользуется своим меню.
Скрипты — в scripts_ui.py.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, Filter, StateFilter
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


class AddDealer(StatesGroup):
    name = State()


class Staff(Filter):
    """Пропускает главного админа и активного диллера; передаёт в обработчик role и dealer."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def __call__(self, event, db: DB) -> bool | dict:
        uid = event.from_user.id
        if uid in self.cfg.admin_ids:
            return {"role": "admin", "dealer": None}
        d = db.dealer_by_tg(uid)
        if d and d["active"]:
            return {"role": "dealer", "dealer": d}
        return False


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
        [KeyboardButton(text=texts.BTN_DEALERS), KeyboardButton(text=texts.BTN_BCAST)],
        [KeyboardButton(text=texts.BTN_HELP)],
    ])


def dealer_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(resize_keyboard=True, keyboard=[
        [KeyboardButton(text=texts.BTN_NEW), KeyboardButton(text=texts.BTN_KEYS)],
        [KeyboardButton(text=texts.BTN_HELP)],
    ])


def staff_keyboard(role: str) -> ReplyKeyboardMarkup:
    return admin_keyboard() if role == "admin" else dealer_keyboard()


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


def name_of(row) -> str:
    return row["name"] if row else "?"


# ======================= ключи: тексты =======================
# scope в кнопках: "a" — все ключи (только админ), "d<id>" — ключи одного диллера
def resolve_scope(role: str, dealer, requested: str = "a") -> tuple[str, Optional[int]]:
    if role == "dealer":                       # диллер всегда видит только свои ключи
        return f"d{dealer['id']}", dealer["id"]
    if requested.startswith("d") and requested[1:].isdigit():
        return requested, int(requested[1:])
    return "a", None


def can_manage(role: str, dealer, c) -> bool:
    return c is not None and (role == "admin" or c["dealer_id"] == dealer["id"])


def key_card(db: DB, c, bot_username: str, scope: str, admin_view: bool):
    who = "ещё не открыл ссылку"
    if c["tg_id"]:
        who = f"<code>{c['tg_id']}</code>" + (f" @{esc(c['tg_username'])}" if c["tg_username"] else "")
    lines = [
        f"🔑 <b>{c['name']}</b> — {'🟢 активен' if c['status'] == 'active' else '🔴 отключён'}",
        f"Сервер: {esc(name_of(db.server(c['server_id'])))} · {esc(name_of(db.script(c['script_id'])))}",
    ]
    if admin_view:
        lines.append(f"Диллер: {esc(name_of(db.dealer(c['dealer_id'])))}" if c["dealer_id"] else "Диллер: — (ваш ключ)")
    lines += [f"Клиент: {who}", f"Срок: {services.fmt_until(c['expires_at'])}",
              f"Создан: {services.fmt_date(c['created_at'])}"]
    if c["note"]:
        lines.append(f"Заметка: {esc(c['note'])}")
    if c["status"] == "active" and not c["tg_id"]:
        lines.append(f"\n🔗 Ссылка для клиента:\n{invite_link(bot_username, c['invite_token'])}")
    i = c["id"]
    back = [("⬅️ К списку", f"kp:0:{scope}")]
    if c["status"] != "active":
        return "\n".join(lines), ikb(back)
    return "\n".join(lines), ikb(
        [("📄 Получить ключ", f"kf:{i}"), ("🔗 Новая ссылка", f"kl:{i}:{scope}")],
        [("+30 дней", f"ke:{i}:30:{scope}"), ("+90 дней", f"ke:{i}:90:{scope}"), ("♾", f"ke:{i}:0:{scope}")],
        [("⛔ Отключить", f"kr:{i}:{scope}")],
        back)


def keys_list(db: DB, page: int, scope: str, dealer_id: Optional[int], admin_view: bool):
    rows = db.list_all(dealer_id)
    servers = db.servers()
    names = {s["id"]: s["name"][:10] for s in servers}
    dealer_names = {d["id"]: d["name"][:8] for d in db.dealers()} if admin_view and dealer_id is None else {}
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    buttons = []
    for c in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = ("🟢" if c["status"] == "active" else "🔴") + ("👤" if c["tg_id"] else "🔗")
        until = "♾" if not c["expires_at"] else services.fmt_date(c["expires_at"])[:10]
        where = f" · {names.get(c['server_id'], '?')}" if len(servers) > 1 else ""
        who = f" · 👥{dealer_names[c['dealer_id']]}" if c["dealer_id"] in dealer_names else ""
        buttons.append([(f"{icon} {c['name']}{where}{who} · {until}", f"k:{c['id']}:{scope}")])
    if pages > 1:
        buttons.append([("◀️", f"kp:{page - 1}:{scope}"), (f"{page + 1}/{pages}", "noop"),
                        ("▶️", f"kp:{page + 1}:{scope}")])
    if not admin_view or dealer_id is None:
        buttons.append([(texts.BTN_NEW, "new")])
    if admin_view and dealer_id is not None:
        buttons.append([("⬅️ К диллеру", f"dl:{dealer_id}")])
    active = sum(1 for c in rows if c["status"] == "active")
    if not admin_view:
        title = "📋 <b>Ваши ключи</b>"
    elif dealer_id is not None:
        title = f"📋 <b>Ключи диллера «{esc(name_of(db.dealer(dealer_id)))}»</b>"
    else:
        title = "📋 <b>Все ключи</b>"
    text = (f"{title}: {len(rows)}, активных: {active}\nНажмите на ключ, чтобы управлять им.\n"
            "🟢 активен · 🔴 отключён · 👤 у клиента · 🔗 ссылку не открыли") if rows else \
        f"{title}\n\nКлючей пока нет. Нажмите «➕ Новый ключ»."
    return text, ikb(*buttons)


# ======================= диллеры: тексты =======================
def dealers_list(db: DB):
    rows = []
    for d in db.dealers():
        active, _ = db.dealer_counts(d["id"])
        icon = "⏸" if not d["active"] else ("👤" if d["tg_id"] else "🔗")
        rows.append([(f"{icon} {d['name']} · 🔑{active}", f"dl:{d['id']}")])
    rows.append([("➕ Добавить диллера", "dla")])
    text = ("👥 <b>Диллеры</b>\nДиллеры создают ключи для своих клиентов и видят только свои ключи.\n"
            "👤 подключён · 🔗 ещё не открыл ссылку · ⏸ заблокирован" if len(rows) > 1 else
            "👥 <b>Диллеров пока нет</b>\n\nНажмите «➕ Добавить диллера» — бот даст ссылку, "
            "диллер откроет её и сможет создавать ключи.")
    return text, ikb(*rows)


def dealer_card(db: DB, did: int, bot_username: str):
    d = db.dealer(did)
    if d is None:
        return "Диллер не найден.", ikb([("⬅️ К диллерам", "dls")])
    active, total = db.dealer_counts(did)
    if d["tg_id"]:
        who = f"<code>{d['tg_id']}</code>" + (f" @{esc(d['tg_username'])}" if d["tg_username"] else "")
    else:
        who = "ещё не открыл ссылку"
    lines = [f"👥 <b>{esc(d['name'])}</b> — {'🟢 активен' if d['active'] else '⏸ заблокирован'}",
             f"Telegram: {who}", f"Ключей: активных <b>{active}</b>, всего {total}",
             f"Добавлен: {services.fmt_date(d['created_at'])}"]
    if not d["tg_id"]:
        lines.append(f"\n🔗 Ссылка для диллера (перешлите ему):\n{invite_link(bot_username, d['invite_token'])}")
    return "\n".join(lines), ikb(
        [("📋 Ключи диллера", f"kp:0:d{did}")],
        [("⏸ Заблокировать", f"dlb:{did}") if d["active"] else ("▶️ Разблокировать", f"dlb:{did}"),
         ("🔗 Новая ссылка", f"dll:{did}")],
        [("🗑 Удалить", f"dld:{did}")],
        [("⬅️ К диллерам", "dls")])


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

    @r.message(F.text == texts.BTN_DEALERS)
    async def menu_dealers(m: Message, state: FSMContext, db: DB):
        await state.clear()
        text, kb = dealers_list(db)
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
                                "Обычно 5–30 минут (на слабом сервере генерация ключей шифрования идёт долго).\nМожно пользоваться ботом — я обновляю это сообщение.\n\n")

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

    # ================= ДИЛЛЕРЫ =================
    @r.callback_query(F.data == "dls")
    async def cb_dealers(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await edit(cb, *dealers_list(db))

    @r.callback_query(F.data.regexp(r"^dl:\d+$"))
    async def cb_dealer(cb: CallbackQuery, db: DB, bot_username: str):
        await cb.answer()
        await edit(cb, *dealer_card(db, int(cb.data[3:]), bot_username))

    @r.callback_query(F.data == "dla")
    async def cb_dealer_add(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(AddDealer.name)
        await cb.message.answer("👥 <b>Новый диллер</b>\n\nКак его назвать? Например: <code>Мерген Ашхабад</code>",
                                reply_markup=ikb(CANCEL))

    @r.message(AddDealer.name, F.text)
    async def dealer_add_name(m: Message, state: FSMContext, db: DB, bot_username: str):
        await state.clear()
        did = db.add_dealer(m.text.strip()[:40])
        d = db.dealer(did)
        await m.answer(f"✅ Диллер <b>{esc(d['name'])}</b> добавлен.\n\n"
                       f"👉 <b>Перешлите ему эту ссылку:</b>\n{invite_link(bot_username, d['invite_token'])}\n\n"
                       "Он откроет её, нажмёт «Запустить» — и сможет создавать ключи. "
                       "Ссылка срабатывает один раз.", disable_web_page_preview=True, reply_markup=admin_keyboard())
        text, kb = dealer_card(db, did, bot_username)
        await m.answer(text, reply_markup=kb, disable_web_page_preview=True)

    @r.callback_query(F.data.startswith("dlb:"))
    async def cb_dealer_block(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str):
        did = int(cb.data[4:])
        d = db.dealer(did)
        db.set_dealer_active(did, not d["active"])
        await cb.answer("Заблокирован" if d["active"] else "Разблокирован")
        if d["tg_id"]:
            if d["active"]:
                await services.safe_send(bot, d["tg_id"], texts.DEALER_BLOCKED)
            else:
                await services.safe_send(bot, d["tg_id"], "✅ Доступ диллера восстановлен.",
                                         reply_markup=dealer_keyboard())
        await edit(cb, *dealer_card(db, did, bot_username))

    @r.callback_query(F.data.startswith("dll:"))
    async def cb_dealer_link(cb: CallbackQuery, db: DB, bot_username: str):
        did = int(cb.data[4:])
        db.reset_dealer_invite(did)
        await cb.answer("Новая ссылка готова. Диллер отвязан, пока не откроет её.", show_alert=True)
        await edit(cb, *dealer_card(db, did, bot_username))

    @r.callback_query(F.data.startswith("dld:"))
    async def cb_dealer_delete_ask(cb: CallbackQuery, db: DB):
        did = int(cb.data[4:])
        active, _ = db.dealer_counts(did)
        await cb.answer()
        await edit(cb, f"🗑 Удалить диллера «{esc(name_of(db.dealer(did)))}»?\n"
                       f"Его ключи ({active} активных) продолжат работать и перейдут вам.",
                   ikb([("Да, удалить", f"dlD:{did}"), ("Нет", f"dl:{did}")]))

    @r.callback_query(F.data.startswith("dlD:"))
    async def cb_dealer_delete(cb: CallbackQuery, db: DB, bot: Bot):
        did = int(cb.data[4:])
        d = db.dealer(did)
        db.delete_dealer(did)
        if d and d["tg_id"]:
            await services.safe_send(bot, d["tg_id"], "⛔ Вы больше не диллер в этом боте.")
        await cb.answer("Диллер удалён")
        await edit(cb, *dealers_list(db))

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

    # любое другое сообщение админа (кроме /start и кнопок меню) — показать меню
    @r.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.in_(texts.MENU_BUTTONS))
    async def admin_fallback(m: Message):
        await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())

    return r


# ======================= КЛЮЧИ: главный админ и диллеры =======================
def build_staff_router(cfg: Config) -> Router:
    r = Router(name="staff")
    staff = Staff(cfg)
    r.message.filter(staff)
    r.callback_query.filter(staff)

    @r.callback_query(F.data == "cancel")
    async def cb_cancel(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.answer()
        await edit(cb, "Отменено.")

    @r.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()

    @r.message(F.text == texts.BTN_HELP)
    async def menu_help(m: Message, state: FSMContext, role: str):
        await state.clear()
        await m.answer(texts.DEALER_HELP, reply_markup=staff_keyboard(role))

    @r.message(F.text == texts.BTN_KEYS)
    async def menu_keys(m: Message, state: FSMContext, db: DB, role: str, dealer):
        await state.clear()
        scope, did = resolve_scope(role, dealer)
        text, kb = keys_list(db, 0, scope, did, role == "admin")
        await m.answer(text, reply_markup=kb)

    # ---------- новый ключ ----------
    @r.message(F.text == texts.BTN_NEW)
    async def menu_new(m: Message, state: FSMContext, db: DB, role: str):
        await state.clear()
        servers, scripts = db.servers(), db.scripts(only_enabled=True)
        if not scripts or not servers:
            if role == "admin":
                hint = ("Сначала добавьте скрипт: «📜 Скрипты» → «⚡ Готовый: OpenVPN»." if not scripts else
                        "Сначала добавьте сервер: «🖥 Серверы» → «➕ Добавить сервер».")
            else:
                hint = "Пока нет доступных серверов. Обратитесь к администратору."
            return await m.answer(hint, reply_markup=staff_keyboard(role))
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
    async def cb_new(cb: CallbackQuery, state: FSMContext, db: DB, role: str):
        await cb.answer()
        await menu_new(cb.message, state, db, role)

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
            return await m.answer(f"❗ Ключ <b>{name}</b> уже есть на этом сервере. Придумайте другое название:",
                                  reply_markup=ikb(CANCEL))
        await state.update_data(name=name, note=note)
        await state.set_state(NewKey.days)
        await m.answer(f"⏳ На какой срок ключ <b>{name}</b>?\nНажмите кнопку или напишите число дней.",
                       reply_markup=ikb([("7 дней", "nd:7"), ("30 дней", "nd:30")],
                                        [("90 дней", "nd:90"), ("♾ Бессрочно", "nd:0")], CANCEL))

    async def create_key(m: Message, state: FSMContext, days: int, db: DB, fleet: Fleet,
                         bot_username: str, role: str, dealer):
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
            return await live.fail(str(e), getattr(e, "output", "") if role == "admin" else "")
        finally:
            fleet.busy.discard(server["id"])
        cid = db.add_client(server["id"], script["id"], name, value, filename, days, data.get("note"),
                            dealer_id=dealer["id"] if role == "dealer" else None)
        c = db.get(cid)
        await status.edit_text(
            f"✅ Ключ <b>{name}</b> создан · {services.fmt_until(c['expires_at'])}\n\n"
            f"👉 <b>Перешлите клиенту эту ссылку:</b>\n{invite_link(bot_username, c['invite_token'])}\n\n"
            "Клиент откроет её, нажмёт «Запустить» — и получит ключ.\n"
            "Сам ключ ниже — если хотите отправить его сами.", disable_web_page_preview=True)
        await services.send_key(m.bot, m.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(NewKey.days, F.data.startswith("nd:"))
    async def new_days_cb(cb: CallbackQuery, state: FSMContext, db: DB, fleet: Fleet, bot_username: str,
                          role: str, dealer):
        await cb.answer()
        await edit(cb, f"Срок: {'бессрочно' if cb.data == 'nd:0' else cb.data[3:] + ' дней'}")
        await create_key(cb.message, state, int(cb.data[3:]), db, fleet, bot_username, role, dealer)

    @r.message(NewKey.days, F.text.regexp(r"^\d{1,4}$"))
    async def new_days_text(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot_username: str,
                            role: str, dealer):
        await create_key(m, state, int(m.text), db, fleet, bot_username, role, dealer)

    # ---------- список и карточка ключа ----------
    @r.callback_query(F.data.startswith("kp:"))
    async def cb_page(cb: CallbackQuery, db: DB, role: str, dealer):
        _, page, requested = (cb.data.split(":") + ["a"])[:3]
        scope, did = resolve_scope(role, dealer, requested)
        await cb.answer()
        await edit(cb, *keys_list(db, int(page), scope, did, role == "admin"))

    async def show_key(cb: CallbackQuery, db: DB, cid: int, scope: str, bot_username: str, role: str, dealer):
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await edit(cb, "Ключ не найден.", ikb([("⬅️ К списку", f"kp:0:{scope}")]))
        await edit(cb, *key_card(db, c, bot_username, scope, role == "admin"))

    def parse(cb: CallbackQuery, role: str, dealer) -> tuple[int, list[str], str]:
        """kX:<id>[:<arg>…]:<scope> → (id, аргументы, scope)."""
        parts = cb.data.split(":")
        requested = parts[-1] if len(parts) > 2 else "a"
        scope, _ = resolve_scope(role, dealer, requested)
        return int(parts[1]), parts[2:-1], scope

    @r.callback_query(F.data.startswith("k:"))
    async def cb_key(cb: CallbackQuery, db: DB, bot_username: str, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        await cb.answer()
        await show_key(cb, db, cid, scope, bot_username, role, dealer)

    @r.callback_query(F.data.startswith("kf:"))
    async def cb_file(cb: CallbackQuery, db: DB, bot: Bot, role: str, dealer):
        c = db.get(int(cb.data.split(":")[1]))
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer()
        await services.send_key(bot, cb.message.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(F.data.startswith("kl:"))
    async def cb_link(cb: CallbackQuery, db: DB, bot_username: str, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        if not can_manage(role, dealer, db.get(cid)):
            return await cb.answer("Ключ не найден", show_alert=True)
        db.reset_invite(cid)
        await cb.answer("Новая ссылка готова. Старая больше не работает.", show_alert=True)
        await show_key(cb, db, cid, scope, bot_username, role, dealer)

    @r.callback_query(F.data.startswith("ke:"))
    async def cb_extend(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str, role: str, dealer):
        cid, args, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c) or c["status"] != "active":
            return await cb.answer("Ключ не активен", show_alert=True)
        until = services.fmt_until(db.extend(cid, int(args[0])))
        await cb.answer(f"Продлено: {until}")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=c["name"], until=until))
        if role == "admin" and c["dealer_id"]:
            await services.tell_dealer(bot, db, c, f"✅ Администратор продлил ключ <b>{c['name']}</b>: {until}.")
        await show_key(cb, db, cid, scope, bot_username, role, dealer)

    @r.callback_query(F.data.startswith("kr:"))
    async def cb_revoke_ask(cb: CallbackQuery, db: DB, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer()
        await edit(cb, f"⛔ Отключить ключ <b>{c['name']}</b>?\n"
                       "Бот удалит его на сервере. Клиент больше не сможет подключиться.",
                   ikb([("Да, отключить", f"kR:{cid}:{scope}"), ("Нет", f"k:{cid}:{scope}")]))

    @r.callback_query(F.data.startswith("kR:"))
    async def cb_revoke(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot, bot_username: str, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer("Отключаю…")
        why = "отключил администратор" if role == "admin" and c["dealer_id"] else None
        try:
            await services.revoke(bot, db, fleet, cid, texts.REVOKED_USER, dealer_why=why)
        except ERRORS as e:
            return await edit(cb, f"❌ Не удалось отключить на сервере: {esc(str(e))}",
                              ikb([("🔄 Ещё раз", f"kR:{cid}:{scope}")], [("⬅️ К ключу", f"k:{cid}:{scope}")]))
        await show_key(cb, db, cid, scope, bot_username, role, dealer)

    # любое другое сообщение диллера — показать меню
    @r.message(StateFilter(None), F.text, ~F.text.startswith("/"))
    async def staff_fallback(m: Message, role: str):
        await m.answer(texts.ADMIN_WELCOME if role == "admin" else texts.DEALER_WELCOME,
                       reply_markup=staff_keyboard(role))

    return r


# ======================= КЛИЕНТ =======================
def build_user_router() -> Router:
    r = Router(name="user")

    @r.message(CommandStart())
    async def cmd_start(m: Message, command: CommandObject, state: FSMContext, db: DB, cfg: Config):
        await state.clear()
        token = (command.args or "").strip()
        uid = m.from_user.id
        if token.startswith("dl_"):  # ссылка диллера
            d = db.dealer_by_token(token)
            if not d or (d["tg_id"] and d["tg_id"] != uid):
                return await m.answer(texts.DEALER_INVITE_INVALID)
            db.bind_dealer(d["id"], uid, m.from_user.username)
            await m.answer(texts.DEALER_JOINED.format(name=esc(d["name"])) + "\n\n" + texts.DEALER_WELCOME,
                           reply_markup=dealer_keyboard())
            who = f" (@{esc(m.from_user.username)})" if m.from_user.username else ""
            return await services.notify_admins(m.bot, cfg, f"👥 Диллер «{esc(d['name'])}» подключился{who}.")
        me_dealer = db.dealer_by_tg(uid)
        if not token:
            if uid in cfg.admin_ids:
                return await m.answer(texts.ADMIN_WELCOME, reply_markup=admin_keyboard())
            if me_dealer:
                if not me_dealer["active"]:
                    return await m.answer(texts.DEALER_BLOCKED)
                return await m.answer(texts.DEALER_WELCOME, reply_markup=dealer_keyboard())
            has = db.clients_of(uid)
            return await m.answer(texts.WELCOME_BACK if has else texts.NO_KEYS, reply_markup=user_keyboard())
        c = db.get_by_token(token)
        if not c:
            return await m.answer(texts.INVITE_INVALID)
        if c["status"] != "active":
            return await m.answer(texts.INVITE_REVOKED)
        if c["tg_id"] and c["tg_id"] != m.from_user.id:
            return await m.answer(texts.INVITE_TAKEN)
        first_time = not c["tg_id"]
        db.bind(c["id"], m.from_user.id, m.from_user.username)
        await m.answer(texts.WELCOME_BOUND.format(name=c["name"]), reply_markup=user_keyboard())
        await services.send_key(m.bot, m.chat.id, c, texts.KEY_CAPTION)
        if first_time and c["dealer_id"]:
            who = f" (@{esc(m.from_user.username)})" if m.from_user.username else ""
            await services.tell_dealer(m.bot, db, c, texts.DEALER_CLIENT_BOUND.format(name=c["name"], who=who))

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
    async def btn_support(m: Message, cfg: Config, db: DB):
        # клиент диллера пишет своему диллеру
        for c in db.clients_of(m.from_user.id):
            d = db.dealer(c["dealer_id"])
            if d and d["active"] and d["tg_username"]:
                return await m.answer(texts.SUPPORT.format(contact="@" + esc(d["tg_username"])))
        await m.answer(texts.SUPPORT.format(contact=esc(cfg.support_contact))
                       if cfg.support_contact else texts.SUPPORT_NONE)

    @r.message()
    async def fallback(m: Message, db: DB):
        d = db.dealer_by_tg(m.from_user.id)
        if d and not d["active"]:
            return await m.answer(texts.DEALER_BLOCKED)
        has = db.clients_of(m.from_user.id)
        await m.answer(texts.WELCOME_BACK if has else texts.NO_KEYS, reply_markup=user_keyboard())

    return r
