"""Админ → 📜 Скрипты: мастер добавления VPN-скрипта и его редактирование (как в VPNCreator)."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from . import presets
from .db import DB
from .steps import ENTER_WORDS, RULE_RE, parse_steps
from .ui import fields, ikb, screen, show
from .ui import nav as nav_row

esc = html.escape
MAX_FILE = 20 * 1024 * 1024
WIZARD = ["name", "file", "install_steps", "check_cmd", "key_steps", "result"]


class ScriptForm(StatesGroup):
    name = State()
    file = State()
    steps = State()
    check = State()
    result_type = State()
    result_path = State()
    result_regex = State()


STEP_FIELDS = {
    "install_steps": ("💻", "Установка", "Выполняется <b>один раз на сервер</b> — при первом ключе "
                                        "или по кнопке «Установить» на сервере."),
    "key_steps": ("🔑", "Создание ключа", "Выполняется <b>каждый раз</b>, когда вы создаёте ключ. "
                                          "Название ключа — <code>{name}</code>."),
    "delete_steps": ("🗑", "Удаление ключа", "Необязательно. Выполняется, когда ключ отключают "
                                             "(вручную или когда закончился срок)."),
    "ip_steps": ("🌐", "Смена IP", "Необязательно. Выполняется при «Сменить IP» на сервере, чтобы новые "
                                  "ключи создавались с новым IP. Подстановки: <code>{old_ip}</code>, "
                                  "<code>{new_ip}</code>, <code>{old_ip_re}</code> (старый IP для sed).\n"
                                  "Пример для OpenVPN:\n<pre>sed -i 's/{old_ip_re}/{new_ip}/g' "
                                  "/etc/openvpn/client-template.txt</pre>"),
}
STEPS_HELP = (
    "Отправляйте <b>всё, что вы напечатали бы в терминале</b> — команды и ответы скрипту, "
    "каждое с новой строки. Можно одним сообщением или по одному.\n"
    "• <code>enter</code> — просто нажать Enter (оставить значение по умолчанию)\n"
    "• бот вводит следующую строку, когда скрипт ждёт ответа\n"
    "• <code>? текст вопроса => ответ</code> — ответ на вопрос, который появляется <b>не всегда</b> "
    "(например <code>? Public IPv4 address => {server_ip}</code> — только на серверах за NAT). "
    "Порядок остальных ответов не сбивается\n"
    "• команды выполняются в папке <code>~/vpnbot</code>, куда загружен файл скрипта"
)
PLACEHOLDERS = ("💡 <b>Подстановки:</b> <code>{name}</code> — название ключа, <code>{file}</code> — путь "
                "к файлу скрипта, <code>{server_ip}</code> — IP сервера.")


def pretty_steps(steps: list[str], limit: int = 2000) -> str:
    if not steps:
        return "<i>пока пусто</i>"
    out, size = [], 0
    for i, s in enumerate(steps, 1):
        if m := RULE_RE.match(s):
            line = f"<code>{i:>2}.</code> 🔎 если спросит «{esc(m[1].strip())}» → <code>{esc(m[2].strip() or 'Enter')}</code>"
        elif s.strip().lower() in ENTER_WORDS:
            line = f"<code>{i:>2}.</code> ⏎ Enter"
        else:
            line = f"<code>{i:>2}. {esc(s)}</code>"
        size += len(line)
        if size > limit:
            out.append(f"… ещё {len(steps) - i + 1}")
            break
        out.append(line)
    return "\n".join(out)


def fmt_size(n: int) -> str:
    return f"{n} Б" if n < 1024 else f"{n / 1024:.1f} КБ"


# ======================= список и карточка =======================
def scripts_list(db: DB, top: str = "") -> tuple[str, InlineKeyboardMarkup]:
    scripts = db.scripts()
    rows = [[(("📜 " if s["enabled"] else "⏸ ") + s["name"], f"sc:v:{s['id']}")] for s in scripts]
    add = [("➕ Добавить скрипт", "sc:new")]
    if not any(s["name"] == "OpenVPN" for s in scripts):
        add.append(("⚡ Готовый: OpenVPN", "sc:preset"))
    rows += [add, nav_row()]
    if not scripts:
        return screen("📜 Скрипты VPN", top, "Скриптов пока нет.",
                      hint="«⚡ Готовый: OpenVPN» — ваш скрипт со всеми шагами уже заполнен"), ikb(*rows)
    return screen("📜 Скрипты VPN", top, fields(("Всего", len(scripts)),
                                                ("Включено", sum(1 for s in scripts if s["enabled"]))),
                  hint="Бот ставит скрипт на сервер и создаёт ключи по его шагам"), ikb(*rows)


def script_card(db: DB, script_id: int, top: str = "") -> tuple[str, InlineKeyboardMarkup] | None:
    s = db.script(script_id)
    if not s:
        return None
    if s["filename"]:
        file = f"{esc(s['filename'])} · {fmt_size(len(s['content'] or b''))}"
    else:
        file = "без файла"
    result = (f"файл <code>{esc(s['result_path'] or '—')}</code>" if s["result_type"] == "file" else
              "вывод команд" + (f" · <code>{esc(s['key_regex'])}</code>" if s["key_regex"] else ""))
    check = f"<code>{esc(s['check_cmd'])}</code>" if s["check_cmd"] else "⚠️ не задана"

    def count(field):
        n = len(parse_steps(s[field]))
        return f"<b>{n}</b> шаг." if n else "—"

    info = fields(("Статус", "✅ Включён" if s["enabled"] else "⏸ Выключен"), ("Файл", file),
                  ("Установлен на серверах", db.script_servers(s["id"], s["version"])), ("Версия", s["version"]))
    if s["source_url"]:
        info += f"\nИсточник: {esc(s['source_url'])}"
    steps = (f"<b>Шаги</b>\n💻 Установка: {count('install_steps')}\n🔎 Проверка: {check}\n"
             f"🔑 Создание ключа: {count('key_steps')}\n📤 Ключ: {result}\n"
             f"🗑 Удаление ключа: {count('delete_steps')}\n🌐 Смена IP: {count('ip_steps')}")
    i = s["id"]
    kb = ikb(
        [("✏️ Название", f"sc:e:{i}:name"), ("📄 Файл", f"sc:e:{i}:file")],
        [("💻 Установка", f"sc:e:{i}:install_steps"), ("🔎 Проверка", f"sc:e:{i}:check_cmd")],
        [("🔑 Создание ключа", f"sc:e:{i}:key_steps"), ("📤 Результат", f"sc:e:{i}:result")],
        [("🗑 Удаление ключа", f"sc:e:{i}:delete_steps"), ("🌐 Смена IP", f"sc:e:{i}:ip_steps")],
        [("👁 Все шаги", f"sc:show:{i}"), ("🔄 Переустановить везде", f"sc:bump:{i}")],
        [("⏸ Выключить" if s["enabled"] else "▶️ Включить", f"sc:tog:{i}"), ("🗑 Удалить", f"sc:del:{i}")],
        nav_row("sc:list"),
    )
    return screen(f"📜 {esc(s['name'])}", top, info, steps, hint="Нажмите на поле, чтобы изменить его"), kb


def build_scripts_router(cfg) -> Router:
    r = Router(name="scripts")
    is_admin = F.from_user.id.in_(cfg.admin_ids)
    r.message.filter(is_admin)
    r.callback_query.filter(is_admin)

    async def show_card(target, db: DB, script_id: int, top: str = ""):
        card = script_card(db, script_id, top)
        if not card:
            return await show(target, *scripts_list(db, top="⚠️ Скрипт не найден"))
        await show(target, *card)

    @r.callback_query(F.data == "sc:list")
    async def cb_list(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show(cb, *scripts_list(db))

    @r.callback_query(F.data.regexp(r"^sc:v:\d+$"))
    async def cb_view(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show_card(cb, db, int(cb.data.split(":")[2]))

    @r.callback_query(F.data.regexp(r"^sc:show:\d+$"))
    async def cb_show(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        await cb.answer()
        blocks = [f"<b>{emoji} {title}</b>\n{pretty_steps(parse_steps(s[field]), 700)}"
                  for field, (emoji, title, _) in STEP_FIELDS.items()]
        await show(cb, screen(f"👁 {esc(s['name'])} · все шаги", *blocks)[:4000], ikb(nav_row(f"sc:v:{s['id']}")))

    @r.callback_query(F.data == "sc:preset")
    async def cb_preset(cb: CallbackQuery, db: DB):
        sid = db.add_script(**presets.openvpn())
        await cb.answer("Готово")
        await show_card(cb, db, sid, top="✅ Готовый скрипт OpenVPN добавлен")

    @r.callback_query(F.data.regexp(r"^sc:tog:\d+$"))
    async def cb_toggle(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        db.update_script(s["id"], enabled=0 if s["enabled"] else 1)
        await cb.answer("Выключен" if s["enabled"] else "Включён")
        await show_card(cb, db, s["id"])

    @r.callback_query(F.data.regexp(r"^sc:bump:\d+$"))
    async def cb_bump(cb: CallbackQuery, db: DB):
        i = int(cb.data.split(":")[2])
        db.bump_script(i)
        await cb.answer("Готово")
        await show_card(cb, db, i, top="🔄 При следующем ключе установка на серверах запустится заново "
                                        "(если «Проверка» не скажет, что всё уже стоит)")

    @r.callback_query(F.data.regexp(r"^sc:del:\d+$"))
    async def cb_delete(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        await cb.answer()
        await show(cb, screen(f"🗑 Удалить скрипт {esc(s['name'])}?", "Созданные ключи у клиентов останутся."),
                   ikb([("🗑 Удалить", f"sc:delok:{s['id']}"), ("✖️ Отмена", f"sc:v:{s['id']}")]))

    @r.callback_query(F.data.regexp(r"^sc:delok:\d+$"))
    async def cb_delete_ok(cb: CallbackQuery, state: FSMContext, db: DB):
        name = esc(db.script(int(cb.data.split(":")[2]))["name"])
        db.delete_script(int(cb.data.split(":")[2]))
        await state.clear()
        await cb.answer("Удалён")
        await show(cb, *scripts_list(db, top=f"🗑 Скрипт {name} удалён"))

    # ======================= мастер / редактирование =======================
    def title(data: dict) -> str:
        if data.get("mode") == "wizard":
            n = WIZARD.index(data["field"] if data["field"] in WIZARD else "result") + 1
            name = data.get("draft", {}).get("name")
            return f"➕ Новый скрипт{' · ' + esc(name) if name else ''} · шаг {n} из {len(WIZARD)}"
        return f"✏️ {esc(data.get('script_name', ''))}"

    def nav(data: dict, *extra) -> InlineKeyboardMarkup:
        wizard = data.get("mode") == "wizard"
        back = [("✖️ Отмена", "sc:list")] if wizard else nav_row(f"sc:v:{data.get('script_id')}")
        return ikb(*[row for row in extra if row], back)

    async def ask(target, state: FSMContext, field: str, error: str = ""):
        data = await state.get_data()
        data["field"] = field
        cur = data.get("current", {})
        err = f"⚠️ {error}" if error else None
        if field == "name":
            await state.set_state(ScriptForm.name)
            body = [("<b>🏷 Название скрипта</b>\nНапример: <code>OpenVPN</code>, <code>WireGuard</code>, "
                     "<code>VLESS</code>"), f"Сейчас: <b>{esc(cur['name'])}</b>" if cur.get("name") else None]
            kb = nav(data)
        elif field == "file":
            await state.set_state(ScriptForm.file)
            body = ["<b>📄 Файл скрипта</b>\n📤 Отправьте файл документом\n🌐 или ссылку на него (GitHub, raw)",
                    f"Сейчас: <b>{esc(cur['filename'])}</b>" if cur.get("filename") else None]
            kb = nav(data, [("Без файла", "sc:f:nofile")])
            hint_ = "Файл загрузится на сервер в ~/vpnbot/ и станет исполняемым"
        elif field in STEP_FIELDS:
            await state.set_state(ScriptForm.steps)
            emoji, name, about = STEP_FIELDS[field]
            steps = data.get("steps", [])
            body = [f"<b>{emoji} {name}</b>\n{about}", STEPS_HELP,
                    f"<b>Шаги ({len(steps)})</b>\n{pretty_steps(steps)}", PLACEHOLDERS]
            more = [("↩️ Убрать последний", "sc:st:undo"), ("🗑 Очистить", "sc:st:clear")] if steps else None
            kb = nav(data, [("✅ Готово", "sc:st:done")], more)
        elif field == "check_cmd":
            await state.set_state(ScriptForm.check)
            body = ["<b>🔎 Проверка установки</b> · необязательно\n"
                    "Команда, которая успешна, если скрипт <b>уже установлен</b> — тогда бот не запустит "
                    "установку повторно.",
                    "⚠️ Обязательно задайте, если скрипт при повторном запуске показывает меню (как OpenVPN).",
                    "Пример для OpenVPN:\n<code>test -f /etc/openvpn/server.conf</code>",
                    f"Сейчас: <code>{esc(cur['check_cmd'])}</code>" if cur.get("check_cmd") else None]
            kb = nav(data, [("Пропустить" if data.get("mode") == "wizard" else "Убрать проверку", "sc:c:skip")])
        else:
            await state.set_state(ScriptForm.result_type)
            body = ["<b>📤 Где взять ключ?</b>",
                    "📄 <b>Файл на сервере</b> — скрипт создаёт файл (например .ovpn), бот скачает его\n"
                    "💻 <b>Вывод команд</b> — ключ печатается в терминал (например vless://…)"]
            kb = nav(data, [("📄 Файл на сервере", "sc:r:file"), ("💻 Вывод команд", "sc:r:output")])
        hint = hint_ if field == "file" else ""
        await state.update_data(field=field)
        await show(target, screen(title(data), *body, err, hint=hint), kb)

    async def field_done(target, state: FSMContext, db: DB, **values):
        data = await state.get_data()
        if data.get("mode") == "wizard":
            draft = {**data.get("draft", {}), **values}
            idx = WIZARD.index(data["field"]) if data["field"] in WIZARD else len(WIZARD) - 1
            await state.update_data(draft=draft, steps=[])
            if data["field"] == "result" or idx + 1 >= len(WIZARD):
                if draft.get("content") is not None:
                    draft["content"] = bytes.fromhex(draft["content"])
                script_id = db.add_script(**draft)
                await state.clear()
                return await show_card(target, db, script_id, top="✅ Скрипт добавлен")
            return await ask(target, state, WIZARD[idx + 1])
        if values.get("content") is not None:
            values["content"] = bytes.fromhex(values["content"])
        db.update_script(data["script_id"], **values)
        await state.clear()
        await show_card(target, db, data["script_id"], top="✅ Сохранено")

    @r.callback_query(F.data == "sc:new")
    async def cb_new(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.update_data(mode="wizard", draft={}, steps=[])
        await cb.answer()
        await ask(cb, state, "name")

    @r.callback_query(F.data.regexp(r"^sc:e:\d+:\w+$"))
    async def cb_edit(cb: CallbackQuery, state: FSMContext, db: DB):
        _, _, i, field = cb.data.split(":")
        s = db.script(int(i))
        await state.clear()
        await state.update_data(mode="edit", script_id=s["id"], script_name=s["name"],
                                steps=parse_steps(s[field]) if field in STEP_FIELDS else [],
                                current={k: s[k] for k in ("name", "filename", "check_cmd")})
        await cb.answer()
        await ask(cb, state, field)

    @r.message(ScriptForm.name, F.text)
    async def in_name(m: Message, state: FSMContext, db: DB):
        await field_done(m, state, db, name=m.text.strip()[:40])

    def raw_github(url: str) -> str:
        mm = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
        return f"https://raw.githubusercontent.com/{mm[1]}/{mm[2]}/{mm[3]}" if mm else url

    def safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64].strip("._") or "script.sh"

    @r.message(ScriptForm.file, F.document)
    async def in_file_doc(m: Message, state: FSMContext, db: DB):
        if m.document.file_size > MAX_FILE:
            return await ask(m, state, "file", "Файл больше 20 МБ")
        content = (await m.bot.download(m.document)).read()
        await field_done(m, state, db, filename=safe_name(m.document.file_name or "script.sh"),
                         content=content.hex(), source_url=None)

    @r.message(ScriptForm.file, F.text)
    async def in_file_url(m: Message, state: FSMContext, db: DB):
        url = raw_github(m.text.strip())
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return await ask(m, state, "file", "Отправьте файл документом или ссылку http(s)://…")
        await show(m, screen(title(await state.get_data()), "⏳ Скачиваю файл…"))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise ValueError(f"сервер ответил {resp.status}")
                    content = await resp.content.read(MAX_FILE + 1)
            if len(content) > MAX_FILE:
                raise ValueError("файл больше 20 МБ")
        except Exception as e:
            return await ask(m, state, "file", f"Не удалось скачать: {esc(str(e))}")
        await field_done(m, state, db, filename=safe_name(parsed.path.rsplit("/", 1)[-1]),
                         content=content.hex(), source_url=url)

    @r.callback_query(ScriptForm.file, F.data == "sc:f:nofile")
    async def cb_nofile(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, filename=None, content=None, source_url=None)

    @r.message(ScriptForm.steps, F.text)
    async def in_steps(m: Message, state: FSMContext):
        data = await state.get_data()
        await state.update_data(steps=data.get("steps", []) + parse_steps(m.text))
        await ask(m, state, data["field"])

    @r.callback_query(ScriptForm.steps, F.data.in_({"sc:st:undo", "sc:st:clear"}))
    async def cb_steps_edit(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        steps = data.get("steps", [])
        await state.update_data(steps=steps[:-1] if cb.data == "sc:st:undo" else [])
        await cb.answer()
        await ask(cb, state, data["field"])

    @r.callback_query(ScriptForm.steps, F.data == "sc:st:done")
    async def cb_steps_done(cb: CallbackQuery, state: FSMContext, db: DB):
        data = await state.get_data()
        steps = data.get("steps", [])
        if not steps and data["field"] == "key_steps":
            return await cb.answer("Добавьте хотя бы один шаг для создания ключа", show_alert=True)
        await cb.answer()
        await field_done(cb, state, db, **{data["field"]: "\n".join(steps)})

    @r.message(ScriptForm.check, F.text)
    async def in_check(m: Message, state: FSMContext, db: DB):
        await field_done(m, state, db, check_cmd=m.text.strip())

    @r.callback_query(ScriptForm.check, F.data == "sc:c:skip")
    async def cb_check_skip(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, check_cmd="")

    def path_screen(data, error=""):
        return screen(title(data), "<b>📄 Путь к файлу ключа</b>\nГде скрипт сохраняет файл? "
                      "Вместо названия ключа пишите <code>{name}</code>",
                      "Пример для OpenVPN:\n<code>/root/{name}.ovpn</code>", f"⚠️ {error}" if error else None), nav(data)

    def regex_screen(data, error=""):
        return screen(title(data), "<b>🔎 Фильтр ключа</b>\nРегулярное выражение — из вывода возьмётся только "
                      "совпадение.", "Пример для VLESS:\n<code>vless://\\S+</code>", f"⚠️ {error}" if error else None,
                      hint="Или нажмите «Весь вывод»"), nav(data, [("Весь вывод", "sc:r:all")])

    @r.callback_query(ScriptForm.result_type, F.data.in_({"sc:r:file", "sc:r:output"}))
    async def cb_result_type(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await cb.answer()
        if cb.data == "sc:r:file":
            await state.set_state(ScriptForm.result_path)
            await show(cb, *path_screen(data))
        else:
            await state.set_state(ScriptForm.result_regex)
            await show(cb, *regex_screen(data))

    @r.message(ScriptForm.result_path, F.text)
    async def in_result_path(m: Message, state: FSMContext, db: DB):
        path = m.text.strip()
        if "\n" in path or len(path) > 300:
            return await show(m, *path_screen(await state.get_data(), "Нужен один путь, например /root/{name}.ovpn"))
        await field_done(m, state, db, result_type="file", result_path=path, key_regex="")

    @r.message(ScriptForm.result_regex, F.text)
    async def in_result_regex(m: Message, state: FSMContext, db: DB):
        try:
            re.compile(m.text.strip())
        except re.error as e:
            return await show(m, *regex_screen(await state.get_data(), f"Некорректное выражение: {esc(str(e))}"))
        await field_done(m, state, db, result_type="output", key_regex=m.text.strip(), result_path="")

    @r.callback_query(ScriptForm.result_regex, F.data == "sc:r:all")
    async def cb_result_all(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, result_type="output", key_regex="", result_path="")

    @r.message(ScriptForm.file)
    @r.message(ScriptForm.steps)
    @r.message(ScriptForm.name)
    @r.message(ScriptForm.check)
    async def in_wrong(m: Message, state: FSMContext):
        data = await state.get_data()
        await ask(m, state, data.get("field", "name"), "Не тот формат — следуйте подсказке")

    return r
