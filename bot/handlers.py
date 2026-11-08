"""Обработчики Telegram.

Роли:
  • главный админ (ADMIN_IDS) — серверы, скрипты, диллеры, рассылка и все ключи;
  • диллер — создаёт ключи и управляет только СВОИМИ ключами;
  • клиент — получает ключ по ссылке-приглашению.

Весь интерфейс — одна «панель» (см. ui.py): экраны меняются на месте, ответы пользователя удаляются.
Роутеры подключаются в порядке: entry → scripts (scripts_ui.py) → admin → staff → client.
"""
from __future__ import annotations

import asyncio
import html
import math
import re
from typing import Optional

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import engine, services, texts
from .config import Config
from .db import DB
from .engine import ERRORS, NAME_RE, Fleet, is_valid_host
from .remote import RemoteAuthError, RemoteError, RemoteHostKeyError, bootstrap_ssh
from .ui import (BTN_HOME, CANCEL, Live, fields, greet, ikb, nav, open_panel, pairs, screen, show)

esc = html.escape
PAGE_SIZE = 8
SSH_TARGET_RE = re.compile(r"^(?:(?P<user>[a-z_][a-z0-9_-]*)@)?(?P<host>[^\s:@]+)(?::(?P<port>\d{1,5}))?$")
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
    password = State()


class Broadcast(StatesGroup):
    text = State()
    confirm = State()


class AddDealer(StatesGroup):
    name = State()


class SetLimit(StatesGroup):
    value = State()


class SupportMsg(StatesGroup):
    text = State()


class SupportReply(StatesGroup):
    text = State()


# ======================= роли =======================
def role_of(cfg: Config, db: DB, uid: int) -> tuple[str, object]:
    """('admin'|'dealer'|'blocked'|'client', строка диллера или None)."""
    if uid in cfg.admin_ids:
        return "admin", None
    d = db.dealer_by_tg(uid)
    if d:
        return ("dealer" if d["active"] else "blocked"), d
    return "client", None


class Staff(Filter):
    """Пропускает главного админа и активного диллера; передаёт в обработчик role и dealer."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def __call__(self, event, db: DB) -> bool | dict:
        role, dealer = role_of(self.cfg, db, event.from_user.id)
        return {"role": role, "dealer": dealer} if role in ("admin", "dealer") else False


def client_label(db: DB, uid: int) -> str:
    for c in db.clients_of(uid):
        if c["tg_username"]:
            return "@" + esc(c["tg_username"])
    return f"ID {uid}"


def name_of(row) -> str:
    return esc(row["name"]) if row else "—"


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"


def short_date(ts) -> str:
    return services.fmt_date(ts)[:10] if ts else "♾"


def limit_text(db: DB, d) -> str:
    active, _ = db.dealer_counts(d["id"])
    return f"{active} из {d['key_limit']}" if d["key_limit"] is not None else f"{active} · без лимита"


def key_status(c) -> str:
    return "🟢 Активен" if c["status"] == "active" else "🔴 Отключён"


# ======================= главные экраны =======================
def home_screen(cfg: Config, db: DB, uid: int):
    role, dealer = role_of(cfg, db, uid)
    if role == "admin":
        servers, scripts = db.servers(), db.scripts(only_enabled=True)
        active = len(db.active())
        dealers = db.dealers()
        blocks = [fields(("🖥 Серверов", len(servers)), ("🔑 Активных ключей", active),
                         ("👥 Диллеров", len(dealers)))]
        if not scripts or not servers or not db.list_all():
            def mark(done):
                return "✅" if done else "▫️"
            blocks.append("<b>🚀 Быстрый старт</b>\n"
                          f"{mark(scripts)} 1. Добавить скрипт VPN — «📜 Скрипты»\n"
                          f"{mark(servers)} 2. Добавить сервер — «🖥 Серверы»\n"
                          f"{mark(db.list_all())} 3. Создать первый ключ — «➕ Новый ключ»")
        kb = ikb([(texts.BTN_NEW, "new")],
                 [(texts.BTN_KEYS, "kp:0:a"), (texts.BTN_SERVERS, "sl")],
                 [(texts.BTN_SCRIPTS, "sc:list"), (texts.BTN_DEALERS, "dls")],
                 [(texts.BTN_BCAST, "bc"), (texts.BTN_HELP, "help")])
        return screen("🏠 Главное меню", *blocks, hint="Выберите раздел"), kb
    if role == "dealer":
        active, total = db.dealer_counts(dealer["id"])
        kb = ikb([(texts.BTN_NEW, "new")],
                 [(texts.BTN_MY_KEYS, f"kp:0:d{dealer['id']}"), (texts.BTN_HELP, "help")])
        return screen("🏠 Главное меню",
                      fields(("👤 Диллер", esc(dealer["name"])), ("🔑 Активных ключей", limit_text(db, dealer)),
                             ("📦 Всего ключей", total)),
                      hint="Выберите раздел"), kb
    if role == "blocked":
        return texts.DEALER_BLOCKED, None
    return client_home(db, uid)


def client_home(db: DB, uid: int):
    keys = db.clients_of(uid)
    if not keys:
        return screen("🔐 Мой VPN", texts.CLIENT_NO_KEYS), ikb([(texts.BTN_HOWTO, "c:howto"),
                                                                (texts.BTN_SUPPORT, "c:support")])
    lines = "\n".join(f"🔑 <b>{c['name']}</b> · {services.fmt_until(c['expires_at'])}" for c in keys)
    kb = ikb([(texts.BTN_GET_KEY, "c:key")], [(texts.BTN_HOWTO, "c:howto"), (texts.BTN_SUPPORT, "c:support")])
    return screen("🔐 Мой VPN", lines, hint="Если IP сервера изменится — бот сам пришлёт новый ключ"), kb


# ======================= ключи =======================
# scope в кнопках: "a" — все ключи (только админ), "d<id>" — ключи одного диллера
def resolve_scope(role: str, dealer, requested: str = "a") -> tuple[str, Optional[int]]:
    if role == "dealer":                       # диллер всегда видит только свои ключи
        return f"d{dealer['id']}", dealer["id"]
    if requested.startswith("d") and requested[1:].isdigit():
        return requested, int(requested[1:])
    return "a", None


def can_manage(role: str, dealer, c) -> bool:
    return c is not None and (role == "admin" or c["dealer_id"] == dealer["id"])


def keys_screen(db: DB, page: int, scope: str, dealer_id: Optional[int], admin_view: bool, top: str = ""):
    rows = db.list_all(dealer_id)
    servers = db.servers()
    srv = {s["id"]: s["name"][:10] for s in servers}
    dlr = {d["id"]: d["name"][:8] for d in db.dealers()} if admin_view and dealer_id is None else {}
    pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    buttons = []
    for c in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        icon = ("🟢" if c["status"] == "active" else "🔴") + ("👤" if c["tg_id"] else "🔗")
        where = f" · {srv.get(c['server_id'], '?')}" if len(servers) > 1 else ""
        who = f" · 👥{dlr[c['dealer_id']]}" if c["dealer_id"] in dlr else ""
        buttons.append([(f"{icon} {c['name']}{where}{who} · {short_date(c['expires_at'])}", f"k:{c['id']}:{scope}")])
    if pages > 1:
        buttons.append([("◀️", f"kp:{page - 1}:{scope}"), (f"{page + 1} / {pages}", "noop"),
                        ("▶️", f"kp:{page + 1}:{scope}")])
    revoked = sum(1 for c in rows if c["status"] != "active")
    if revoked:
        buttons.append([(f"🧹 Удалить отключённые ({revoked})", f"kx:{scope}")])
    if admin_view and dealer_id is not None:
        title = f"📋 Ключи диллера {name_of(db.dealer(dealer_id))}"
        buttons.append(nav(f"dl:{dealer_id}"))
    else:
        title = "📋 Все ключи" if admin_view else "📋 Мои ключи"
        buttons.append([(texts.BTN_NEW, "new")])
        buttons.append(nav())
    if not rows:
        return screen(title, top, "Ключей пока нет.", hint="Создайте первый: «➕ Новый ключ»"), ikb(*buttons)
    active = sum(1 for c in rows if c["status"] == "active")
    return screen(title, top, fields(("Всего", len(rows)), ("Активных", active)),
                  hint="🟢 активен · 🔴 отключён · 👤 у клиента · 🔗 ссылку ещё не открыли"), ikb(*buttons)


def key_screen(db: DB, c, bot_username: str, scope: str, admin_view: bool, top: str = ""):
    client = "<i>ещё не открыл ссылку</i>"
    if c["tg_id"]:
        client = f"@{esc(c['tg_username'])}" if c["tg_username"] else f"ID {c['tg_id']}"
    info = fields(
        ("Статус", key_status(c)),
        ("Сервер", name_of(db.server(c["server_id"]))),
        ("VPN", name_of(db.script(c["script_id"]))),
        ("Диллер", (name_of(db.dealer(c["dealer_id"])) if c["dealer_id"] else "— (ваш)") if admin_view else None),
        ("Срок", services.fmt_until(c["expires_at"])),
        ("Создан", services.fmt_date(c["created_at"])[:10]),
        ("Заметка", esc(c["note"]) if c["note"] else None),
    ) + f"\nКлиент: <b>{client}</b>"
    link = None
    if c["status"] == "active" and not c["tg_id"]:
        link = f"<b>🔗 Ссылка для клиента</b>\n{invite_link(bot_username, c['invite_token'])}"
    i = c["id"]
    back = nav(f"kp:0:{scope}")
    if c["status"] != "active":
        return screen(f"🔑 {c['name']}", top, info), ikb([("🗑 Удалить ключ", f"kd:{i}:{scope}")], back)
    kb = ikb([("📄 Файл ключа", f"kf:{i}"), ("🔗 Новая ссылка", f"kl:{i}:{scope}")],
             [("+30 дней", f"ke:{i}:30:{scope}"), ("+90 дней", f"ke:{i}:90:{scope}"), ("♾ Бессрочно", f"ke:{i}:0:{scope}")],
             [("⛔ Отключить", f"kr:{i}:{scope}"), ("🗑 Удалить", f"kd:{i}:{scope}")],
             back)
    hint = "Перешлите ссылку клиенту — он нажмёт «Запустить» и получит ключ" if link else ""
    return screen(f"🔑 {c['name']}", top, info, link, hint=hint), kb


# ======================= серверы =======================
def servers_screen(db: DB, top: str = ""):
    servers = db.servers()
    rows = [[(f"🖥 {s['name']} · {s['host']} · 🔑 {db.count_active(s['id'])}", f"s:{s['id']}")] for s in servers]
    rows += [[("➕ Добавить сервер", "sa")], nav()]
    if not servers:
        return screen("🖥 Серверы", top, "Серверов пока нет.",
                      hint="Нажмите «➕ Добавить сервер» — понадобятся только IP и пароль root"), ikb(*rows)
    return screen("🖥 Серверы", top, fields(("Всего", len(servers))), hint="Нажмите на сервер, чтобы управлять им"), ikb(*rows)


async def server_screen(db: DB, fleet: Fleet, sid: int, top: str = ""):
    s = db.server(sid)
    if s is None:
        return screen("🖥 Сервер не найден"), ikb(nav("sl"))
    error = ""
    if sid not in fleet.busy:
        try:
            await fleet.remote(s).ping(timeout=8)   # карточка открывается быстро, даже если сервер «мёртв»
        except RemoteError as e:
            error = str(e)
    busy = sid in fleet.busy
    status = "⏳ Идёт установка или перенос" if busy else ("⚠️ Нет связи" if error else "✅ На связи")
    info = fields(("Статус", status), ("IP", s["host"]), ("SSH", f"{s['user']}@{s['host']}:{s['port']}"),
                  ("Активных ключей", db.count_active(sid)))
    if error:
        info += f"\n<i>{esc(error)}</i>\n\n💡 Сменился IP сервера? Нажмите «✏️ Сменить IP»"
    rows: list = []
    vpn_lines = []
    for sc in db.scripts(only_enabled=True):
        ver = db.installed_version(sid, sc["id"])
        if ver == sc["version"]:
            vpn_lines.append(f"✅ {esc(sc['name'])} — установлен")
        elif ver is None:
            vpn_lines.append(f"▫️ {esc(sc['name'])} — не установлен")
            rows.append([(f"⚙️ Установить {sc['name']}", f"si:{sid}:{sc['id']}")])
        else:
            vpn_lines.append(f"🔄 {esc(sc['name'])} — скрипт изменился")
            rows.append([(f"🔄 Переустановить {sc['name']}", f"si:{sid}:{sc['id']}")])
    vpn = "<b>VPN на сервере</b>\n" + ("\n".join(vpn_lines) or "<i>скриптов нет — добавьте в «📜 Скрипты»</i>")
    if busy:
        rows = []
    else:
        rows.append([("✏️ Сменить IP", f"sc_ip:{sid}"), ("🔄 Разослать ключи", f"sp:{sid}")])
        if db.count_active(sid) and len(db.servers()) > 1:
            rows.append([("🔁 Перенести клиентов", f"st:{sid}")])
    rows.append([("♻️ Обновить", f"s:{sid}"), ("🗑 Удалить", f"sd:{sid}")])
    rows.append(nav("sl"))
    hint = "Не установленный VPN поставится сам при первом ключе" if any("▫️" in l for l in vpn_lines) else ""
    return screen(f"🖥 {esc(s['name'])}", top, info, vpn, hint=hint), ikb(*rows)


# ======================= диллеры =======================
def dealers_screen(db: DB, top: str = ""):
    rows = []
    for d in db.dealers():
        active, _ = db.dealer_counts(d["id"])
        icon = "⏸" if not d["active"] else ("👤" if d["tg_id"] else "🔗")
        lim = f"/{d['key_limit']}" if d["key_limit"] is not None else ""
        rows.append([(f"{icon} {d['name']} · 🔑 {active}{lim}", f"dl:{d['id']}")])
    count = len(rows)
    rows += [[("➕ Добавить диллера", "dla")], nav()]
    if not count:
        return screen("👥 Диллеры", top, "Диллеров пока нет.",
                      hint="Диллер создаёт ключи для своих клиентов и видит только свои ключи"), ikb(*rows)
    return screen("👥 Диллеры", top, fields(("Всего", count)),
                  hint="👤 подключён · 🔗 ещё не открыл ссылку · ⏸ заблокирован"), ikb(*rows)


def dealer_screen(db: DB, did: int, bot_username: str, top: str = ""):
    d = db.dealer(did)
    if d is None:
        return screen("👥 Диллер не найден"), ikb(nav("dls"))
    active, total = db.dealer_counts(did)
    tg = (f"@{esc(d['tg_username'])}" if d["tg_username"] else f"ID {d['tg_id']}") if d["tg_id"] else None
    info = fields(("Статус", "🟢 Активен" if d["active"] else "⏸ Заблокирован"),
                  ("Telegram", tg or "ещё не открыл ссылку"),
                  ("Активных ключей", limit_text(db, d)), ("Всего ключей", total),
                  ("Добавлен", services.fmt_date(d["created_at"])[:10]))
    link = None if d["tg_id"] else f"<b>🔗 Ссылка для диллера</b>\n{invite_link(bot_username, d['invite_token'])}"
    kb = ikb([("📋 Ключи диллера", f"kp:0:d{did}"), ("🎚 Лимит ключей", f"dlm:{did}")],
             [("⏸ Заблокировать", f"dlb:{did}") if d["active"] else ("▶️ Разблокировать", f"dlb:{did}"),
              ("🔗 Новая ссылка", f"dll:{did}")],
             [("🗑 Удалить диллера", f"dld:{did}")],
             nav("dls"))
    hint = "Перешлите ссылку диллеру — он нажмёт «Запустить» и сможет создавать ключи" if link else ""
    return screen(f"👥 {esc(d['name'])}", top, info, link, hint=hint), kb


# ======================= ВХОД: /start, «Главное меню», ссылки =======================
def build_entry_router(cfg: Config) -> Router:
    r = Router(name="entry")

    @r.message(CommandStart())
    async def cmd_start(m: Message, command: CommandObject, state: FSMContext, db: DB):
        await state.clear()
        uid = m.from_user.id
        token = (command.args or "").strip()
        top = ""
        key_to_send = None
        if token.startswith("dl_"):                          # ссылка диллера
            d = db.dealer_by_token(token)
            if not d or (d["tg_id"] and d["tg_id"] != uid):
                top = f"⚠️ {texts.DEALER_INVITE_INVALID}"
            else:
                db.bind_dealer(d["id"], uid, m.from_user.username)
                top = texts.DEALER_JOINED.format(name=esc(d["name"]))
                who = f" · @{esc(m.from_user.username)}" if m.from_user.username else ""
                await services.notify_admins(m.bot, cfg, f"👥 <b>Диллер {esc(d['name'])} подключился</b>{who}")
        elif token:                                          # ссылка клиента
            c = db.get_by_token(token)
            if not c:
                top = f"⚠️ {texts.INVITE_INVALID}"
            elif c["status"] != "active":
                top = f"⚠️ {texts.INVITE_REVOKED}"
            elif c["tg_id"] and c["tg_id"] != uid:
                top = f"⚠️ {texts.INVITE_TAKEN}"
            else:
                first_time = not c["tg_id"]
                db.bind(c["id"], uid, m.from_user.username)
                top = texts.CLIENT_BOUND.format(name=c["name"])
                key_to_send = c
                if first_time and c["dealer_id"]:
                    who = f" · @{esc(m.from_user.username)}" if m.from_user.username else ""
                    await services.tell_dealer(m.bot, db, c, texts.DEALER_CLIENT_BOUND.format(name=c["name"], who=who))
        role, _ = role_of(cfg, db, uid)
        try:
            await m.delete()
        except Exception:
            pass
        await greet(m.bot, m.chat.id, {"admin": texts.GREET_ADMIN, "dealer": texts.GREET_DEALER}
                    .get(role, texts.GREET_CLIENT))
        text, kb = home_screen(cfg, db, uid)
        await open_panel(m.bot, m.chat.id, f"{top}\n\n{text}" if top else text, kb)
        if key_to_send is not None:                          # клиенту — файл ключа под меню
            await services.send_key(m.bot, m.chat.id, key_to_send, texts.KEY_CAPTION)

    @r.message(F.text == BTN_HOME)
    @r.message(F.text.in_(texts.LEGACY_BUTTONS))
    async def btn_home(m: Message, state: FSMContext, db: DB):
        await state.clear()
        legacy = m.text != BTN_HOME
        text, kb = home_screen(cfg, db, m.from_user.id)
        try:
            await m.delete()
        except Exception:
            pass
        if legacy:  # заменить старую нижнюю клавиатуру на новую
            role, _ = role_of(cfg, db, m.from_user.id)
            await greet(m.bot, m.chat.id, {"admin": texts.GREET_ADMIN, "dealer": texts.GREET_DEALER}
                        .get(role, texts.GREET_CLIENT))
        await open_panel(m.bot, m.chat.id, text, kb)

    @r.callback_query(F.data.in_({"home", "cancel"}))
    async def cb_home(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show(cb, *home_screen(cfg, db, cb.from_user.id))

    @r.callback_query(F.data == "noop")
    async def cb_noop(cb: CallbackQuery):
        await cb.answer()

    return r


# ======================= ГЛАВНЫЙ АДМИН =======================
def build_admin_router(cfg: Config) -> Router:
    r = Router(name="admin")
    is_admin = F.from_user.id.in_(cfg.admin_ids)
    r.message.filter(is_admin)
    r.callback_query.filter(is_admin)

    # ================= СЕРВЕРЫ =================
    @r.callback_query(F.data == "sl")
    async def cb_servers(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show(cb, *servers_screen(db))

    @r.callback_query(F.data.regexp(r"^s:\d+$"))
    async def cb_server(cb: CallbackQuery, state: FSMContext, db: DB, fleet: Fleet):
        await state.clear()
        await cb.answer("Проверяю сервер…")
        await show(cb, *await server_screen(db, fleet, int(cb.data[2:])))

    # ---------- добавить сервер ----------
    def add_server_screen(step: int, question: str, data: dict, error: str = ""):
        done = fields(("Название", esc(data["name"]) if data.get("name") else None),
                      ("IP", f"{data['host']}:{data['port']}" if data.get("host") else None))
        return screen(f"🖥 Новый сервер · шаг {step} из 3", done or None, question,
                      f"⚠️ {error}" if error else None), ikb(CANCEL)

    @r.callback_query(F.data == "sa")
    async def cb_add(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(AddServer.name)
        await show(cb, *add_server_screen(1, "Как назвать сервер?\nНапример: <code>Германия-1</code>", {}))

    @r.message(AddServer.name, F.text)
    async def add_name(m: Message, state: FSMContext):
        await state.update_data(name=m.text.strip()[:40])
        await state.set_state(AddServer.host)
        await show(m, *add_server_screen(2, "Напишите <b>IP сервера</b>, например <code>1.2.3.4</code>\n"
                                            "<i>Если SSH-порт не 22: 1.2.3.4:2222</i>", await state.get_data()))

    @r.message(AddServer.host, F.text)
    async def add_host(m: Message, state: FSMContext):
        match = SSH_TARGET_RE.match(m.text.strip())
        data = await state.get_data()
        if not match or not is_valid_host(match["host"]) or int(match["port"] or 22) > 65535:
            return await show(m, *add_server_screen(2, "Напишите <b>IP сервера</b>, например <code>1.2.3.4</code>",
                                                    data, "Это не похоже на IP-адрес"))
        user = match["user"] or "root"
        await state.update_data(host=match["host"], port=int(match["port"] or 22), user=user)
        await state.set_state(AddServer.password)
        await show(m, *add_server_screen(3, f"Напишите <b>пароль {user}</b> от сервера.\n"
                                            "<i>🔒 Бот зайдёт один раз, добавит свой ключ и сразу удалит сообщение "
                                            "с паролем. Пароль нигде не сохраняется.</i>", await state.get_data()))

    @r.message(AddServer.password, F.text)
    async def add_password(m: Message, state: FSMContext, db: DB, fleet: Fleet):
        password = m.text
        data = await state.get_data()
        panel = await show(m, *add_server_screen(3, f"⏳ Подключаюсь к <b>{data['host']}</b>…", data))
        try:
            host_key = await bootstrap_ssh(data["host"], data["port"], data["user"], password, fleet.pubkey)
        except RemoteError as e:
            text, kb = add_server_screen(3, f"Напишите <b>пароль {data['user']}</b> ещё раз.", data, str(e))
            return await panel.edit_text(text, reply_markup=kb)
        await state.clear()
        sid = db.add_server(data["name"], "ssh", data["host"], data["port"], data["user"], host_key)
        text, kb = await server_screen(db, fleet, sid, top="✅ Сервер добавлен")
        await panel.edit_text(text, reply_markup=kb)

    # ---------- установка скрипта ----------
    @r.callback_query(F.data.startswith("si:"))
    async def cb_install(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        _, sid, scid = cb.data.split(":")
        server, script = db.server(int(sid)), db.script(int(scid))
        if server["id"] in fleet.busy:
            return await cb.answer("На сервере уже идёт операция", show_alert=True)
        await cb.answer("Установка запущена")
        fleet.busy.add(server["id"])
        await show(cb, *await server_screen(db, fleet, server["id"]))
        # прогресс — отдельным сообщением, чтобы меню оставалось свободным
        status = await bot.send_message(cb.message.chat.id, "⏳")
        live = Live(status, screen(f"⚙️ Установка {esc(script['name'])} · {esc(server['name'])}",
                                   hint="Обычно 5–30 минут. Можно пользоваться ботом — это сообщение обновляется"))

        async def job():
            try:
                async with fleet.remote(server).busy:
                    await engine.install(fleet, db, server, script, live, force=True)
                await live.done(f"🎉 <b>{esc(script['name'])} установлен</b>\nТеперь можно создавать ключи.",
                                ikb([(texts.BTN_NEW, "new"), ("🖥 К серверу", f"s:{server['id']}")]))
            except ERRORS as e:
                await live.fail(str(e), getattr(e, "output", ""),
                                ikb([("🔄 Повторить", cb.data), ("🖥 К серверу", f"s:{server['id']}")]))
            finally:
                fleet.busy.discard(server["id"])
        background(job())

    # ---------- рассылка ключей сервера ----------
    @r.callback_query(F.data.startswith("sp:"))
    async def cb_push(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        sid = int(cb.data[3:])
        await cb.answer("Рассылаю…")
        ok, fail = await services.push_configs(bot, db, sid, texts.PUSH_CAPTION)
        await show(cb, *await server_screen(db, fleet, sid, top=f"✅ Ключи разосланы: {ok} · не доставлено: {fail}"))

    # ---------- смена IP ----------
    def ip_screen(server, error: str = ""):
        return screen(f"✏️ Смена IP · {esc(server['name'])}", fields(("Сейчас", server["host"])),
                      "Напишите <b>новый IP</b> сервера.",
                      f"⚠️ {esc(error)}" if error else None,
                      hint="Бот подключится по новому IP, обновит ключи и сам разошлёт их клиентам и диллерам"), \
            ikb(nav(f"s:{server['id']}"))

    def password_screen(server, host: str, reason: str, error: str = ""):
        return screen(f"🔐 Смена IP · {esc(server['name'])}",
                      fields(("Новый IP", host)),
                      f"⚠️ {esc(reason)}.",
                      f"Напишите <b>пароль {server['user']}</b> от сервера на новом IP — бот подключится заново.\n"
                      "<i>🔒 Сообщение с паролем сразу удалится, пароль не сохраняется.</i>",
                      f"❌ {esc(error)}" if error else None,
                      hint="На AWS при смене IP часто создаётся новый сервер — поэтому бот не может войти по старому ключу"), \
            ikb(nav(f"s:{server['id']}"))

    @r.callback_query(F.data.startswith("sc_ip:"))
    async def cb_change_ip(cb: CallbackQuery, state: FSMContext, db: DB):
        sid = int(cb.data.split(":")[1])
        await cb.answer()
        await state.set_state(ChangeIP.host)
        await state.update_data(sid=sid)
        await show(cb, *ip_screen(db.server(sid)))

    @r.message(ChangeIP.host, F.text)
    async def change_ip_text(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot: Bot):
        host = m.text.strip().lower()
        sid = (await state.get_data())["sid"]
        server = db.server(sid)
        if not is_valid_host(host):
            return await show(m, *ip_screen(server, "Это не похоже на IP-адрес"))
        if host == server["host"]:
            return await show(m, *ip_screen(server, "Это текущий IP сервера"))
        panel = await show(m, screen(f"🌐 Смена IP · {esc(server['name'])}", f"⏳ Подключаюсь к <b>{esc(host)}</b>…"))
        try:
            old = await services.switch_host(db, fleet, sid, host)
        except (RemoteAuthError, RemoteHostKeyError) as e:  # новый сервер / ключ бота пропал — нужен пароль
            await state.update_data(new_host=host, reason=str(e))
            await state.set_state(ChangeIP.password)
            text, kb = password_screen(server, host, str(e))
            return await panel.edit_text(text, reply_markup=kb)
        except RemoteError as e:                            # не отвечает и т.п. — можно ввести другой IP
            text, kb = ip_screen(server, str(e))
            return await panel.edit_text(text, reply_markup=kb)
        await state.clear()
        await after_switch(panel, db, fleet, bot, sid, old)

    @r.message(ChangeIP.password, F.text)
    async def change_ip_password(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot: Bot):
        data = await state.get_data()
        sid, host = data["sid"], data["new_host"]
        server = db.server(sid)
        panel = await show(m, screen(f"🔐 Смена IP · {esc(server['name'])}", f"⏳ Подключаюсь к <b>{esc(host)}</b>…"))
        try:
            host_key = await bootstrap_ssh(host, server["port"], server["user"], m.text, fleet.pubkey)
            old = await services.switch_host(db, fleet, sid, host, host_key)
        except RemoteError as e:
            text, kb = password_screen(server, host, data["reason"], str(e))
            return await panel.edit_text(text, reply_markup=kb)
        await state.clear()
        await after_switch(panel, db, fleet, bot, sid, old)

    async def after_switch(panel: Message, db: DB, fleet: Fleet, bot: Bot, sid: int, old: str):
        """IP уже переключён. Тот же сервер — обновляем ключи; новый (VPN нет) — предлагаем пересоздать."""
        server = db.server(sid)
        try:
            missing = await services.missing_vpn(db, fleet, sid)
        except ERRORS:
            missing = []
        if missing:
            n = db.count_active(sid)
            text = screen(f"🆕 Новый сервер · {esc(server['name'])}",
                          fields(("Новый IP", server["host"]), ("Клиентов", n)),
                          f"Бот подключился, но на сервере нет VPN ({esc(', '.join(missing))}) — "
                          "значит, это новый сервер, и старые ключи на нём работать не будут.",
                          "<b>Что сделает бот:</b>\n1. Установит VPN заново\n2. Создаст новые ключи всем клиентам "
                          "с теми же именами и сроками\n3. Сам отправит их клиентам и диллерам",
                          hint="Установка займёт 5–30 минут")
            return await panel.edit_text(text, reply_markup=ikb(
                [(f"✅ Установить VPN и пересоздать ключи ({n})", f"rb:{sid}")], nav(f"s:{sid}")))
        header = screen(f"🌐 Смена IP · {esc(server['name'])}", f"<code>{old}</code> → <code>{server['host']}</code>")
        live = Live(panel, header)
        try:
            updated, ok, fail, warnings = await services.change_address(bot, db, fleet, sid, old, live)
        except ERRORS as e:
            return await live.fail(str(e), getattr(e, "output", ""), ikb(nav(f"s:{sid}")))
        warn = "".join(f"\n⚠️ {esc(w)}" for w in warnings)
        await live.done("✅ <b>Готово</b>\n" + fields(("Обновлено ключей", updated), ("Отправлено клиентам", ok),
                                                     ("Не доставлено", fail)) + warn,
                        ikb([("🖥 К серверу", f"s:{sid}")], nav()))

    @r.callback_query(F.data.startswith("rb:"))
    async def cb_rebuild(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        sid = int(cb.data[3:])
        if sid in fleet.busy:
            return await cb.answer("На сервере уже идёт операция", show_alert=True)
        await cb.answer("Запущено")
        fleet.busy.add(sid)
        server = db.server(sid)
        await show(cb, *await server_screen(db, fleet, sid))
        status = await bot.send_message(cb.message.chat.id, "⏳")
        live = Live(status, screen(f"🆕 Новый сервер · {esc(server['name'])}",
                                   hint="Установка VPN и пересоздание ключей. Это сообщение обновляется"))

        async def job():
            try:
                ok, errors = await services.rebuild_server(bot, db, fleet, sid, live)
                err = "".join(f"\n❌ {esc(x[:150])}" for x in errors[:10])
                await live.done(f"✅ <b>Готово: ключей пересоздано {ok}</b>\nКлиенты и диллеры уже получили новые файлы.{err}",
                                ikb([("🖥 К серверу", f"s:{sid}")], nav()))
            except Exception as e:
                await live.fail(f"Не удалось: {e}", getattr(e, "output", ""),
                                ikb([("🔄 Повторить", f"rb:{sid}")], nav(f"s:{sid}")))
            finally:
                fleet.busy.discard(sid)
        background(job())

    # ---------- перенос клиентов ----------
    @r.callback_query(F.data.startswith("st:"))
    async def cb_transfer_pick(cb: CallbackQuery, db: DB):
        src = int(cb.data[3:])
        await cb.answer()
        others = [(f"🖥 {s['name']}", f"sT:{src}:{s['id']}") for s in db.servers() if s["id"] != src]
        await show(cb, screen(f"🔁 Перенос клиентов · {name_of(db.server(src))}",
                              "Куда перенести клиентов?",
                              hint="Каждый клиент получит новый ключ от выбранного сервера, сроки сохранятся"),
                   ikb(*pairs(others), nav(f"s:{src}")))

    @r.callback_query(F.data.startswith("sT:"))
    async def cb_transfer_confirm(cb: CallbackQuery, db: DB):
        _, src, dst = cb.data.split(":")
        await cb.answer()
        await show(cb, screen("🔁 Перенос клиентов",
                              fields(("Откуда", name_of(db.server(int(src)))), ("Куда", name_of(db.server(int(dst)))),
                                     ("Клиентов", db.count_active(int(src)))),
                              hint="Старые ключи будут отключены, если старый сервер доступен"),
                   ikb([("✅ Перенести", f"sY:{src}:{dst}"), ("✖️ Отмена", f"s:{src}")]))

    @r.callback_query(F.data.startswith("sY:"))
    async def cb_transfer(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        _, src, dst = cb.data.split(":")
        src, dst = int(src), int(dst)
        if {src, dst} & fleet.busy:
            return await cb.answer("Сервер сейчас занят", show_alert=True)
        await cb.answer("Перенос запущен")
        fleet.busy.update({src, dst})
        await show(cb, *await server_screen(db, fleet, src))
        status = await bot.send_message(cb.message.chat.id, "⏳")
        live = Live(status, screen(f"🔁 Перенос клиентов → {name_of(db.server(dst))}",
                                   hint="Это сообщение обновляется"))

        async def job():
            try:
                ok, errors = await services.transfer_clients(bot, db, fleet, src, dst, live)
                err = "".join(f"\n❌ {esc(x[:150])}" for x in errors[:10])
                await live.done(f"✅ <b>Перенесено клиентов: {ok}</b>\nОни уже получили новые ключи.{err}",
                                ikb([("🖥 К серверу", f"s:{dst}")]))
            except Exception as e:
                await live.fail(f"Перенос не удался: {e}")
            finally:
                fleet.busy.difference_update({src, dst})
        background(job())

    # ---------- удаление сервера ----------
    @r.callback_query(F.data.startswith("sd:"))
    async def cb_delete_ask(cb: CallbackQuery, db: DB):
        sid = int(cb.data[3:])
        await cb.answer()
        n = db.count_active(sid)
        await show(cb, screen(f"🗑 Удалить сервер {name_of(db.server(sid))}?",
                              f"Активных ключей: <b>{n}</b> — они будут отключены в боте, клиенты получат уведомление."
                              if n else None,
                              hint="На самом сервере ничего не удаляется"),
                   ikb([("🗑 Удалить", f"sD:{sid}"), ("✖️ Отмена", f"s:{sid}")]))

    @r.callback_query(F.data.startswith("sD:"))
    async def cb_delete(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot):
        name = name_of(db.server(int(cb.data[3:])))
        await services.delete_server(bot, db, fleet, int(cb.data[3:]))
        await cb.answer("Сервер удалён")
        await show(cb, *servers_screen(db, top=f"🗑 Сервер {name} удалён"))

    # ================= ДИЛЛЕРЫ =================
    @r.callback_query(F.data == "dls")
    async def cb_dealers(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show(cb, *dealers_screen(db))

    @r.callback_query(F.data.regexp(r"^dl:\d+$"))
    async def cb_dealer(cb: CallbackQuery, db: DB, bot_username: str):
        await cb.answer()
        await show(cb, *dealer_screen(db, int(cb.data[3:]), bot_username))

    @r.callback_query(F.data == "dla")
    async def cb_dealer_add(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(AddDealer.name)
        await show(cb, screen("👥 Новый диллер", "Как его назвать?\nНапример: <code>Мерген Ашхабад</code>"),
                   ikb(CANCEL))

    @r.message(AddDealer.name, F.text)
    async def dealer_add_name(m: Message, state: FSMContext, db: DB, bot_username: str):
        await state.clear()
        did = db.add_dealer(m.text.strip()[:40])
        await show(m, *dealer_screen(db, did, bot_username, top="✅ Диллер добавлен"))

    @r.callback_query(F.data.startswith("dlb:"))
    async def cb_dealer_block(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str):
        did = int(cb.data[4:])
        d = db.dealer(did)
        db.set_dealer_active(did, not d["active"])
        await cb.answer("Заблокирован" if d["active"] else "Разблокирован")
        if d["tg_id"]:
            await services.safe_send(bot, d["tg_id"], texts.DEALER_BLOCKED if d["active"] else texts.DEALER_UNBLOCKED)
        await show(cb, *dealer_screen(db, did, bot_username))

    def limit_screen(db: DB, did: int, error: str = ""):
        d = db.dealer(did)
        active, _ = db.dealer_counts(did)
        return screen(f"🎚 Лимит ключей · {esc(d['name'])}",
                      fields(("Сейчас", d["key_limit"] if d["key_limit"] is not None else "без лимита"),
                             ("Активных ключей", active)),
                      "Сколько <b>активных</b> ключей может быть у диллера?\n"
                      "<i>Нажмите кнопку или напишите число</i>",
                      f"⚠️ {error}" if error else None,
                      hint="Отключённые и истёкшие ключи место не занимают. Продление лимит не тратит"), \
            ikb([("5", f"dlms:{did}:5"), ("10", f"dlms:{did}:10"), ("20", f"dlms:{did}:20"), ("50", f"dlms:{did}:50")],
                [("♾ Без лимита", f"dlms:{did}:-1")], nav(f"dl:{did}"))

    async def apply_limit(db: DB, bot: Bot, did: int, limit):
        db.set_dealer_limit(did, limit)
        d = db.dealer(did)
        if d["tg_id"]:
            await services.safe_send(bot, d["tg_id"], texts.DEALER_LIMIT_CHANGED.format(
                limit=f"{limit} активных ключей" if limit is not None else "без лимита"))
        return "без лимита" if limit is None else str(limit)

    @r.callback_query(F.data.startswith("dlm:"))
    async def cb_dealer_limit(cb: CallbackQuery, state: FSMContext, db: DB):
        did = int(cb.data[4:])
        await cb.answer()
        await state.set_state(SetLimit.value)
        await state.update_data(did=did)
        await show(cb, *limit_screen(db, did))

    @r.callback_query(F.data.startswith("dlms:"))
    async def cb_dealer_limit_set(cb: CallbackQuery, state: FSMContext, db: DB, bot: Bot, bot_username: str):
        _, did, value = cb.data.split(":")
        await state.clear()
        shown = await apply_limit(db, bot, int(did), None if value == "-1" else int(value))
        await cb.answer("Сохранено")
        await show(cb, *dealer_screen(db, int(did), bot_username, top=f"🎚 Лимит ключей: {shown}"))

    @r.message(SetLimit.value, F.text)
    async def dealer_limit_text(m: Message, state: FSMContext, db: DB, bot: Bot, bot_username: str):
        did = (await state.get_data())["did"]
        if not m.text.strip().isdigit() or int(m.text.strip()) > 100000:
            return await show(m, *limit_screen(db, did, "Напишите число, например 15"))
        await state.clear()
        shown = await apply_limit(db, bot, did, int(m.text.strip()))
        await show(m, *dealer_screen(db, did, bot_username, top=f"🎚 Лимит ключей: {shown}"))

    @r.callback_query(F.data.startswith("dll:"))
    async def cb_dealer_link(cb: CallbackQuery, db: DB, bot_username: str):
        did = int(cb.data[4:])
        db.reset_dealer_invite(did)
        await cb.answer("Готово")
        await show(cb, *dealer_screen(db, did, bot_username,
                                      top="🔗 Новая ссылка готова — диллер отвязан, пока не откроет её"))

    @r.callback_query(F.data.startswith("dld:"))
    async def cb_dealer_delete_ask(cb: CallbackQuery, db: DB):
        did = int(cb.data[4:])
        active, _ = db.dealer_counts(did)
        await cb.answer()
        await show(cb, screen(f"🗑 Удалить диллера {name_of(db.dealer(did))}?",
                              f"Его ключи (активных: <b>{active}</b>) продолжат работать и перейдут вам."),
                   ikb([("🗑 Удалить", f"dlD:{did}"), ("✖️ Отмена", f"dl:{did}")]))

    @r.callback_query(F.data.startswith("dlD:"))
    async def cb_dealer_delete(cb: CallbackQuery, db: DB, bot: Bot):
        did = int(cb.data[4:])
        d = db.dealer(did)
        db.delete_dealer(did)
        if d and d["tg_id"]:
            await services.safe_send(bot, d["tg_id"], texts.DEALER_REMOVED)
        await cb.answer("Диллер удалён")
        await show(cb, *dealers_screen(db, top=f"🗑 Диллер {name_of(d)} удалён"))

    # ================= РАССЫЛКА =================
    @r.callback_query(F.data == "bc")
    async def cb_bcast(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await state.set_state(Broadcast.text)
        await show(cb, screen("📢 Рассылка", fields(("Получателей", len(db.user_ids()))),
                              "Напишите сообщение — его получат все клиенты с активным ключом."), ikb(CANCEL))

    @r.message(Broadcast.text, F.text)
    async def bcast_text(m: Message, state: FSMContext, db: DB):
        await state.update_data(text=m.text)
        await state.set_state(Broadcast.confirm)
        await show(m, screen("📢 Рассылка · проверьте", f"📢 {esc(m.text)}",
                             fields(("Получателей", len(db.user_ids())))),
                   ikb([("✅ Отправить", "bc_go"), ("✏️ Изменить", "bc")], CANCEL))

    @r.callback_query(Broadcast.confirm, F.data == "bc_go")
    async def bcast_go(cb: CallbackQuery, state: FSMContext, db: DB, bot: Bot):
        text = (await state.get_data())["text"]
        await state.clear()
        await cb.answer("Отправляю…")
        ok = fail = 0
        for uid in db.user_ids():
            if await services.safe_send(bot, uid, f"📢 {esc(text)}"):
                ok += 1
            else:
                fail += 1
            await asyncio.sleep(0.05)
        await show(cb, screen("📢 Рассылка отправлена", fields(("Доставлено", ok), ("Не доставлено", fail))),
                   ikb(nav()))

    return r


# ======================= КЛЮЧИ: главный админ и диллеры =======================
def build_staff_router(cfg: Config) -> Router:
    r = Router(name="staff")
    staff = Staff(cfg)
    r.message.filter(staff)
    r.callback_query.filter(staff)

    @r.callback_query(F.data == "help")
    async def cb_help(cb: CallbackQuery, role: str):
        await cb.answer()
        text = texts.HELP_ADMIN.format(warn=cfg.warn_days) if role == "admin" else texts.HELP_DEALER
        await show(cb, screen("❓ Помощь", text), ikb(nav()))

    # ---------- новый ключ ----------
    def new_key_screen(db: DB, data: dict, question: str, error: str = "", kb_rows=()):
        done = fields(("Сервер", name_of(db.server(data["sid"])) if data.get("sid") and len(db.servers()) > 1 else None),
                      ("VPN", name_of(db.script(data["scid"])) if data.get("scid") and len(db.scripts(True)) > 1 else None),
                      ("Ключ", data.get("name")))
        return screen("➕ Новый ключ", done or None, question, f"⚠️ {error}" if error else None), ikb(*kb_rows, CANCEL)

    NAME_Q = ("Напишите <b>имя ключа</b> латиницей, например <code>ahmet</code>\n"
              "<i>Можно добавить заметку через пробел: ahmet Ахмет +99361234567</i>")
    DAYS_Q = "На какой срок?\n<i>Нажмите кнопку или напишите число дней</i>"
    DAYS_ROWS = ([("7 дней", "nd:7"), ("30 дней", "nd:30")], [("90 дней", "nd:90"), ("♾ Бессрочно", "nd:0")])

    @r.callback_query(F.data == "new")
    async def cb_new(cb: CallbackQuery, state: FSMContext, db: DB, role: str, dealer):
        await state.clear()
        await cb.answer()
        if role == "dealer" and db.dealer_slots_left(dealer["id"]) == 0:
            active, _ = db.dealer_counts(dealer["id"])
            return await show(cb, screen("➕ Новый ключ", texts.DEALER_LIMIT_REACHED.format(
                active=active, limit=dealer["key_limit"])), ikb([(texts.BTN_MY_KEYS, f"kp:0:d{dealer['id']}")], nav()))
        servers, scripts = db.servers(), db.scripts(only_enabled=True)
        if not scripts or not servers:
            if role == "admin":
                hint = ("Сначала добавьте скрипт VPN: «📜 Скрипты» → «⚡ Готовый: OpenVPN»" if not scripts else
                        "Сначала добавьте сервер: «🖥 Серверы» → «➕ Добавить сервер»")
            else:
                hint = "Пока нет доступных серверов. Обратитесь к администратору"
            return await show(cb, screen("➕ Новый ключ", hint), ikb(nav()))
        if len(servers) == 1:
            await state.update_data(sid=servers[0]["id"])
            return await ask_script(cb, state, db)
        await state.set_state(NewKey.server)
        await show(cb, *new_key_screen(db, {}, "На каком сервере создать ключ?",
                                       kb_rows=pairs([(f"🖥 {s['name']}", f"ns:{s['id']}") for s in servers])))

    async def ask_script(event, state: FSMContext, db: DB):
        scripts = db.scripts(only_enabled=True)
        if len(scripts) == 1:
            await state.update_data(scid=scripts[0]["id"])
            await state.set_state(NewKey.name)
            return await show(event, *new_key_screen(db, await state.get_data(), NAME_Q))
        await state.set_state(NewKey.script)
        await show(event, *new_key_screen(db, await state.get_data(), "Какой VPN?",
                                          kb_rows=pairs([(f"📜 {s['name']}", f"nsc:{s['id']}") for s in scripts])))

    @r.callback_query(NewKey.server, F.data.startswith("ns:"))
    async def new_server(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await state.update_data(sid=int(cb.data[3:]))
        await ask_script(cb, state, db)

    @r.callback_query(NewKey.script, F.data.startswith("nsc:"))
    async def new_script(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await state.update_data(scid=int(cb.data[4:]))
        await state.set_state(NewKey.name)
        await show(cb, *new_key_screen(db, await state.get_data(), NAME_Q))

    @r.message(NewKey.name, F.text)
    async def new_name(m: Message, state: FSMContext, db: DB):
        parts = m.text.strip().split(maxsplit=1)
        name, note = parts[0], (parts[1] if len(parts) > 1 else None)
        data = await state.get_data()
        if not NAME_RE.match(name):
            return await show(m, *new_key_screen(db, data, NAME_Q, "Только латинские буквы, цифры, «_» и «-», "
                                                                   "без пробелов, до 32 символов"))
        old = db.get_by_name(data["sid"], name)
        if old and old["status"] == "active":
            return await show(m, *new_key_screen(db, data, NAME_Q, f"Ключ {name} уже есть — придумайте другое имя"))
        await state.update_data(name=name, note=note)
        await state.set_state(NewKey.days)
        await show(m, *new_key_screen(db, await state.get_data(), DAYS_Q, kb_rows=DAYS_ROWS))

    async def create_key(event, state: FSMContext, days: int, db: DB, fleet: Fleet,
                         bot_username: str, role: str, dealer):
        data = await state.get_data()
        await state.clear()
        server, script, name = db.server(data["sid"]), db.script(data["scid"]), data["name"]
        if role == "dealer" and db.dealer_slots_left(dealer["id"]) == 0:   # лимит мог кончиться, пока вводили имя
            active, _ = db.dealer_counts(dealer["id"])
            return await show(event, screen("➕ Новый ключ", texts.DEALER_LIMIT_REACHED.format(
                active=active, limit=db.dealer(dealer["id"])["key_limit"])), ikb(nav()))
        header = screen(f"🔑 Создание ключа {name}", fields(("Сервер", name_of(server)), ("VPN", name_of(script))))
        panel = await show(event, header)
        live = Live(panel, header)
        await live("⏳ Подключение к серверу…")
        fleet.busy.add(server["id"])
        try:
            value, filename = await engine.create_key(fleet, db, server, script, name, live)
        except ERRORS as e:
            return await live.fail(str(e), getattr(e, "output", "") if role == "admin" else "",
                                   ikb([("🔄 Попробовать снова", "new")], nav()))
        finally:
            fleet.busy.discard(server["id"])
        cid = db.add_client(server["id"], script["id"], name, value, filename, days, data.get("note"),
                            dealer_id=dealer["id"] if role == "dealer" else None)
        c = db.get(cid)
        scope, _ = resolve_scope(role, dealer)
        text, kb = key_screen(db, c, bot_username, scope, role == "admin", top="✅ Ключ создан")
        try:
            await live.msg.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            live.msg = await open_panel(live.msg.bot, live.msg.chat.id, text, kb)
        await services.send_key(live.msg.bot, live.msg.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(NewKey.days, F.data.startswith("nd:"))
    async def new_days_cb(cb: CallbackQuery, state: FSMContext, db: DB, fleet: Fleet, bot_username: str,
                          role: str, dealer):
        await cb.answer()
        await create_key(cb, state, int(cb.data[3:]), db, fleet, bot_username, role, dealer)

    @r.message(NewKey.days, F.text.regexp(r"^\d{1,4}$"))
    async def new_days_text(m: Message, state: FSMContext, db: DB, fleet: Fleet, bot_username: str,
                            role: str, dealer):
        await create_key(m, state, int(m.text), db, fleet, bot_username, role, dealer)

    @r.message(NewKey.days)
    async def new_days_bad(m: Message, state: FSMContext, db: DB):
        await show(m, *new_key_screen(db, await state.get_data(), DAYS_Q,
                                      "Нажмите кнопку или напишите число дней (0 — бессрочно)", DAYS_ROWS))

    # ---------- удаление ключей ----------
    @r.callback_query(F.data.startswith("kd:"))
    async def cb_delete_ask(cb: CallbackQuery, db: DB, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer()
        what = ("Бот удалит ключ на сервере и из бота — клиент больше не сможет подключиться."
                if c["status"] == "active" else "Ключ уже отключён — он просто исчезнет из списка.")
        await show(cb, screen(f"🗑 Удалить ключ {c['name']}?", what, hint="Это нельзя отменить"),
                   ikb([("🗑 Удалить", f"kD:{cid}:{scope}"), ("✖️ Отмена", f"k:{cid}:{scope}")]))

    async def finish_delete(cb: CallbackQuery, db: DB, bot: Bot, c, scope: str, role: str, dealer, notify: bool):
        if notify and c["status"] == "active":   # сервер недоступен — сообщаем сами
            if c["tg_id"]:
                await services.safe_send(bot, c["tg_id"], texts.REVOKED_USER.format(name=c["name"]))
            if role == "admin" and c["dealer_id"]:
                await services.tell_dealer(bot, db, c, texts.DEALER_KEY_OFF.format(
                    name=c["name"], why="удалил администратор"))
        db.delete_client(c["id"])
        _, did = resolve_scope(role, dealer, scope)
        await show(cb, *keys_screen(db, 0, scope, did, role == "admin", top=f"🗑 Ключ {c['name']} удалён"))

    @r.callback_query(F.data.startswith("kD:"))
    async def cb_delete(cb: CallbackQuery, db: DB, fleet: Fleet, bot: Bot, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer("Удаляю…")
        if c["status"] == "active":
            why = "удалил администратор" if role == "admin" and c["dealer_id"] else None
            try:
                await services.revoke(bot, db, fleet, cid, texts.REVOKED_USER, dealer_why=why)
            except ERRORS as e:
                return await show(cb, screen(f"🗑 Ключ {c['name']}",
                                             f"⚠️ Не удалось удалить на сервере:\n{esc(str(e))}",
                                             hint="Можно удалить только из бота — на сервере ключ останется рабочим, "
                                                  "пока сервер недоступен"),
                                  ikb([("🔄 Повторить", f"kD:{cid}:{scope}")],
                                      [("🗑 Удалить только из бота", f"kF:{cid}:{scope}")], nav(f"k:{cid}:{scope}")))
        await finish_delete(cb, db, bot, db.get(cid), scope, role, dealer, notify=False)

    @r.callback_query(F.data.startswith("kF:"))
    async def cb_delete_force(cb: CallbackQuery, db: DB, bot: Bot, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer("Удалено")
        await finish_delete(cb, db, bot, c, scope, role, dealer, notify=True)

    @r.callback_query(F.data.startswith("kx:"))
    async def cb_purge_ask(cb: CallbackQuery, db: DB, role: str, dealer):
        scope, did = resolve_scope(role, dealer, cb.data[3:])
        n = sum(1 for c in db.list_all(did) if c["status"] != "active")
        await cb.answer()
        await show(cb, screen(f"🧹 Удалить отключённые ключи ({n})?",
                              "Отключённые и истёкшие ключи исчезнут из списка. Активные ключи не затрагиваются."),
                   ikb([("🧹 Удалить", f"kX:{scope}"), ("✖️ Отмена", f"kp:0:{scope}")]))

    @r.callback_query(F.data.startswith("kX:"))
    async def cb_purge(cb: CallbackQuery, db: DB, role: str, dealer):
        scope, did = resolve_scope(role, dealer, cb.data[3:])
        n = db.delete_revoked(did)
        await cb.answer("Готово")
        await show(cb, *keys_screen(db, 0, scope, did, role == "admin", top=f"🧹 Удалено отключённых ключей: {n}"))

    # ---------- поддержка: ответ клиенту ----------
    def can_reply(db: DB, role: str, dealer, client_uid: int) -> bool:
        return role == "admin"   # поддержка — только главный админ

    @r.callback_query(F.data.startswith("rep:"))
    async def cb_reply(cb: CallbackQuery, state: FSMContext, db: DB, role: str, dealer):
        uid = int(cb.data[4:])
        if not can_reply(db, role, dealer, uid):
            return await cb.answer("Это не ваш клиент", show_alert=True)
        await cb.answer()
        await state.set_state(SupportReply.text)
        await state.update_data(uid=uid, who=client_label(db, uid))
        await open_panel(cb.bot, cb.message.chat.id,
                         texts.SUPPORT_REPLY_ASK.format(who=client_label(db, uid)), ikb(CANCEL))

    @r.message(SupportReply.text)
    async def reply_text(m: Message, state: FSMContext, db: DB, bot: Bot):
        data = await state.get_data()
        await state.clear()
        kb = ikb([(texts.BTN_WRITE, "c:write")])
        try:
            await bot.send_message(data["uid"], texts.SUPPORT_REPLY_HEADER)
            await m.copy_to(data["uid"], reply_markup=kb)
            text = texts.SUPPORT_REPLY_SENT.format(who=data["who"])
        except (TelegramBadRequest, TelegramForbiddenError):
            text = f"⚠️ Не удалось доставить ответ {data['who']} — клиент заблокировал бота."
        await open_panel(bot, m.chat.id, text, ikb(nav()))

    # ---------- список и карточка ключа ----------
    @r.callback_query(F.data.startswith("kp:"))
    async def cb_page(cb: CallbackQuery, state: FSMContext, db: DB, role: str, dealer):
        await state.clear()
        _, page, requested = (cb.data.split(":") + ["a"])[:3]
        scope, did = resolve_scope(role, dealer, requested)
        await cb.answer()
        await show(cb, *keys_screen(db, int(page), scope, did, role == "admin"))

    async def show_key(cb: CallbackQuery, db: DB, cid: int, scope: str, bot_username: str, role: str, dealer,
                       top: str = ""):
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await show(cb, screen("🔑 Ключ не найден"), ikb(nav(f"kp:0:{scope}")))
        await show(cb, *key_screen(db, c, bot_username, scope, role == "admin", top))

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
        await cb.answer("Отправляю файл")
        await services.send_key(bot, cb.message.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(F.data.startswith("kl:"))
    async def cb_link(cb: CallbackQuery, db: DB, bot_username: str, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        if not can_manage(role, dealer, db.get(cid)):
            return await cb.answer("Ключ не найден", show_alert=True)
        db.reset_invite(cid)
        await cb.answer("Готово")
        await show_key(cb, db, cid, scope, bot_username, role, dealer,
                       top="🔗 Новая ссылка готова — старая больше не работает")

    @r.callback_query(F.data.startswith("ke:"))
    async def cb_extend(cb: CallbackQuery, db: DB, bot: Bot, bot_username: str, role: str, dealer):
        cid, args, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c) or c["status"] != "active":
            return await cb.answer("Ключ не активен", show_alert=True)
        until = services.fmt_until(db.extend(cid, int(args[0])))
        await cb.answer("Продлено")
        if c["tg_id"]:
            await services.safe_send(bot, c["tg_id"], texts.EXTENDED_USER.format(name=c["name"], until=until))
        if role == "admin" and c["dealer_id"]:
            await services.tell_dealer(bot, db, c, texts.DEALER_EXTENDED.format(name=c["name"], until=until))
        await show_key(cb, db, cid, scope, bot_username, role, dealer, top=f"✅ Продлено: {until}")

    @r.callback_query(F.data.startswith("kr:"))
    async def cb_revoke_ask(cb: CallbackQuery, db: DB, role: str, dealer):
        cid, _, scope = parse(cb, role, dealer)
        c = db.get(cid)
        if not can_manage(role, dealer, c):
            return await cb.answer("Ключ не найден", show_alert=True)
        await cb.answer()
        await show(cb, screen(f"⛔ Отключить ключ {c['name']}?",
                              "Бот удалит ключ на сервере — клиент больше не сможет подключиться.",
                              hint="Это нельзя отменить"),
                   ikb([("⛔ Отключить", f"kR:{cid}:{scope}"), ("✖️ Отмена", f"k:{cid}:{scope}")]))

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
            return await show(cb, screen(f"⛔ Ключ {c['name']}", f"⚠️ Не удалось отключить на сервере:\n{esc(str(e))}"),
                              ikb([("🔄 Повторить", f"kR:{cid}:{scope}")], nav(f"k:{cid}:{scope}")))
        await show_key(cb, db, cid, scope, bot_username, role, dealer, top="⛔ Ключ отключён")

    return r


# ======================= КЛИЕНТ =======================
def build_client_router(cfg: Config) -> Router:
    r = Router(name="client")

    @r.callback_query(F.data == "c:key")
    async def cb_key(cb: CallbackQuery, db: DB, bot: Bot):
        keys = db.clients_of(cb.from_user.id)
        await cb.answer("Отправляю ключ" if keys else "Ключей нет")
        for c in keys:
            await services.send_key(bot, cb.message.chat.id, c, texts.KEY_CAPTION)

    @r.callback_query(F.data == "c:howto")
    async def cb_howto(cb: CallbackQuery):
        await cb.answer()
        await show(cb, screen("📲 Как подключиться", texts.HOWTO, hint=texts.HOWTO_HINT), ikb(nav()))

    def support_targets(db: DB, uid: int) -> tuple[list[int], Optional[str]]:
        """Вопросы клиентов (в том числе клиентов диллеров) всегда идут главному админу.
        → (кому, @username для кнопки «Открыть чат» — из SUPPORT_CONTACT, если там не числовой ID)."""
        contact = cfg.support_contact.lstrip("@") if cfg.support_contact and not cfg.support_contact.lstrip("@").isdigit() else None
        return list(cfg.admin_ids), contact

    @r.callback_query(F.data == "c:support")
    async def cb_support(cb: CallbackQuery, db: DB):
        await cb.answer()
        _, username = support_targets(db, cb.from_user.id)
        rows = [[(texts.BTN_WRITE, "c:write")]]
        blocks = [texts.SUPPORT]
        markup = ikb(*rows, nav())
        if username:
            blocks.append(texts.SUPPORT_CONTACT.format(contact="@" + esc(username)))
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=texts.BTN_WRITE, callback_data="c:write")],
                [InlineKeyboardButton(text=f"💬 Открыть чат @{username}", url=f"https://t.me/{username}")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="home")]])
        await show(cb, screen("🆘 Поддержка", *blocks), markup)

    @r.callback_query(F.data == "c:write")
    async def cb_write(cb: CallbackQuery, state: FSMContext):
        await cb.answer()
        await state.set_state(SupportMsg.text)
        await open_panel(cb.bot, cb.message.chat.id, screen("✍️ Вопрос в поддержку", texts.SUPPORT_ASK), ikb(CANCEL))

    @r.message(SupportMsg.text)
    async def support_text(m: Message, state: FSMContext, db: DB, bot: Bot):
        await state.clear()
        targets, _ = support_targets(db, m.from_user.id)
        keys = ", ".join(c["name"] for c in db.clients_of(m.from_user.id)) or "нет"
        header = texts.SUPPORT_TO_STAFF.format(who=client_label(db, m.from_user.id), keys=esc(keys))
        delivered = 0
        for t in targets:
            try:
                await bot.send_message(t, header, reply_markup=ikb([("↩️ Ответить", f"rep:{m.from_user.id}")]))
                await m.copy_to(t)
                delivered += 1
            except (TelegramBadRequest, TelegramForbiddenError):
                pass
        text = texts.SUPPORT_SENT if delivered else "⚠️ Не удалось отправить. Попробуйте позже."
        await open_panel(bot, m.chat.id, screen("🆘 Поддержка", text), ikb(nav()))

    @r.message()
    async def fallback(m: Message, db: DB):
        text, kb = home_screen(cfg, db, m.from_user.id)
        try:
            await m.delete()
        except Exception:
            pass
        await open_panel(m.bot, m.chat.id, text, kb)

    return r
