"""Админ → 📜 Скрипты: мастер добавления VPN-скрипта и его редактирование (как в VPNCreator)."""
from __future__ import annotations

import html
import re
from urllib.parse import urlparse

import aiohttp
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from . import presets, texts
from .db import DB
from .steps import ENTER_WORDS, RULE_RE, parse_steps
from .ui import edit, ikb

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
def scripts_list(db: DB) -> tuple[str, InlineKeyboardMarkup]:
    scripts = db.scripts()
    rows = [[(("📜 " if s["enabled"] else "⏸ ") + s["name"] + ("" if s["enabled"] else " (выкл.)"),
              f"sc:v:{s['id']}")] for s in scripts]
    rows.append([("➕ Добавить скрипт", "sc:new")])
    if not any(s["name"] == "OpenVPN" for s in scripts):
        rows.append([("⚡ Готовый: OpenVPN (ваш скрипт)", "sc:preset")])
    text = ("📜 <b>Скрипты VPN</b>\n\n"
            + ("Скриптов пока нет. Добавьте первый — бот проведёт по шагам.\n"
               "Или нажмите «⚡ Готовый: OpenVPN» — всё уже заполнено." if not scripts else
               "Бот ставит скрипт на сервер и создаёт ключи по его шагам.\n"
               "Если включено несколько скриптов — при создании ключа бот спросит, какой."))
    return text, ikb(*rows)


def script_card(db: DB, script_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    s = db.script(script_id)
    if not s:
        return None
    if s["filename"]:
        file = f"<code>{esc(s['filename'])}</code> · {fmt_size(len(s['content'] or b''))}"
        if s["source_url"]:
            file += f"\n      🌐 {esc(s['source_url'])}"
    else:
        file = "<i>без файла</i>"
    result = (f"файл <code>{esc(s['result_path'] or '—')}</code>" if s["result_type"] == "file" else
              "вывод команд" + (f", фильтр <code>{esc(s['key_regex'])}</code>" if s["key_regex"] else ""))
    check = f"<code>{esc(s['check_cmd'])}</code>" if s["check_cmd"] else "⚠️ не задана"
    text = (
        f"📜 <b>{esc(s['name'])}</b>  {'✅' if s['enabled'] else '⏸ выключен'}\n\n"
        f"📄 Файл: {file}\n"
        f"💻 Установка: <b>{len(parse_steps(s['install_steps']))}</b> шаг.\n"
        f"🔎 Проверка: {check}\n"
        f"🔑 Создание ключа: <b>{len(parse_steps(s['key_steps']))}</b> шаг.\n"
        f"📤 Ключ: {result}\n"
        f"🗑 Удаление ключа: {len(parse_steps(s['delete_steps'])) or '—'}\n"
        f"🌐 Смена IP: {len(parse_steps(s['ip_steps'])) or '—'}\n"
        f"🖥 Установлен на серверах: <b>{db.script_servers(s['id'], s['version'])}</b> · версия {s['version']}"
    )
    i = s["id"]
    kb = ikb(
        [("✏️ Название", f"sc:e:{i}:name"), ("📄 Файл", f"sc:e:{i}:file")],
        [("💻 Установка", f"sc:e:{i}:install_steps"), ("🔎 Проверка", f"sc:e:{i}:check_cmd")],
        [("🔑 Создание ключа", f"sc:e:{i}:key_steps"), ("📤 Результат", f"sc:e:{i}:result")],
        [("🗑 Удаление ключа", f"sc:e:{i}:delete_steps"), ("🌐 Смена IP", f"sc:e:{i}:ip_steps")],
        [("⏸ Выключить" if s["enabled"] else "▶️ Включить", f"sc:tog:{i}"),
         ("🔄 Переустановить везде", f"sc:bump:{i}")],
        [("👁 Показать все шаги", f"sc:show:{i}")],
        [("❌ Удалить скрипт", f"sc:del:{i}")],
        [("⬅️ К скриптам", "sc:list")],
    )
    return text, kb


def build_scripts_router(cfg) -> Router:
    r = Router(name="scripts")
    is_admin = F.from_user.id.in_(cfg.admin_ids)
    # кнопки главного меню обрабатывает основной роутер — их нельзя принять за шаг скрипта
    r.message.filter(is_admin, ~F.text.in_(texts.MENU_BUTTONS))
    r.callback_query.filter(is_admin)

    async def show_card(target, db: DB, script_id: int):
        card = script_card(db, script_id)
        if not card:
            return await (target.answer("Скрипт не найден", show_alert=True) if isinstance(target, CallbackQuery)
                          else target.answer("Скрипт не найден"))
        if isinstance(target, CallbackQuery):
            await edit(target, *card)
        else:
            await target.answer(card[0], reply_markup=card[1], disable_web_page_preview=True)

    @r.callback_query(F.data == "sc:list")
    async def cb_list(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await edit(cb, *scripts_list(db))

    @r.callback_query(F.data.regexp(r"^sc:v:\d+$"))
    async def cb_view(cb: CallbackQuery, state: FSMContext, db: DB):
        await state.clear()
        await cb.answer()
        await show_card(cb, db, int(cb.data.split(":")[2]))

    @r.callback_query(F.data.regexp(r"^sc:show:\d+$"))
    async def cb_show(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        await cb.answer()
        parts = [f"📜 <b>{esc(s['name'])}</b> — все шаги"]
        for field, (emoji, title, _) in STEP_FIELDS.items():
            parts.append(f"\n{emoji} <b>{title}</b>\n{pretty_steps(parse_steps(s[field]), 900)}")
        await cb.message.answer("\n".join(parts)[:4000])

    @r.callback_query(F.data == "sc:preset")
    async def cb_preset(cb: CallbackQuery, db: DB):
        sid = db.add_script(**presets.openvpn())
        await cb.answer("Готовый скрипт OpenVPN добавлен")
        await show_card(cb, db, sid)

    @r.callback_query(F.data.regexp(r"^sc:tog:\d+$"))
    async def cb_toggle(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        db.update_script(s["id"], enabled=0 if s["enabled"] else 1)
        await cb.answer()
        await show_card(cb, db, s["id"])

    @r.callback_query(F.data.regexp(r"^sc:bump:\d+$"))
    async def cb_bump(cb: CallbackQuery, db: DB):
        i = int(cb.data.split(":")[2])
        db.bump_script(i)
        await cb.answer("При следующем ключе на каждом сервере установка запустится заново "
                        "(если «Проверка» не скажет, что всё уже установлено)", show_alert=True)
        await show_card(cb, db, i)

    @r.callback_query(F.data.regexp(r"^sc:del:\d+$"))
    async def cb_delete(cb: CallbackQuery, db: DB):
        s = db.script(int(cb.data.split(":")[2]))
        await cb.answer()
        await edit(cb, f"⚠️ Удалить скрипт <b>{esc(s['name'])}</b>?\n\nСозданные ключи у клиентов останутся.",
                   ikb([("Да, удалить", f"sc:delok:{s['id']}"), ("Нет", f"sc:v:{s['id']}")]))

    @r.callback_query(F.data.regexp(r"^sc:delok:\d+$"))
    async def cb_delete_ok(cb: CallbackQuery, state: FSMContext, db: DB):
        db.delete_script(int(cb.data.split(":")[2]))
        await cb_list(cb, state, db)

    # ======================= мастер / редактирование =======================
    def header(data: dict) -> str:
        if data.get("mode") == "wizard":
            n = WIZARD.index(data["field"] if data["field"] in WIZARD else "result") + 1
            name = data.get("draft", {}).get("name")
            return f"➕ <b>Новый скрипт</b>{' · ' + esc(name) if name else ''} · шаг {n}/{len(WIZARD)}\n\n"
        return f"✏️ <b>{esc(data.get('script_name', ''))}</b>\n\n"

    def nav(data: dict, *extra) -> InlineKeyboardMarkup:
        wizard = data.get("mode") == "wizard"
        back = ("❌ Отмена", "sc:list") if wizard else ("⬅️ Назад", f"sc:v:{data.get('script_id')}")
        return ikb(*[row for row in extra if row], [back])

    async def ask(target, state: FSMContext, field: str):
        data = await state.get_data()
        data["field"] = field
        cur = data.get("current", {})
        if field == "name":
            await state.set_state(ScriptForm.name)
            text = ("🏷 <b>Название скрипта</b>\n\nНапример: <code>OpenVPN</code>, <code>WireGuard</code>, "
                    "<code>VLESS</code>." + (f"\n\nСейчас: <b>{esc(cur['name'])}</b>" if cur.get("name") else ""))
            kb = nav(data)
        elif field == "file":
            await state.set_state(ScriptForm.file)
            text = ("📄 <b>Файл скрипта</b>\n\n📤 Отправьте файл документом\n🌐 или ссылку на него "
                    "(GitHub, raw-ссылка)\n\nФайл загрузится на сервер в <code>~/vpnbot/</code> и станет "
                    "исполняемым." + (f"\n\nСейчас: <code>{esc(cur['filename'])}</code>" if cur.get("filename") else ""))
            kb = nav(data, [("Без файла", "sc:f:nofile")])
        elif field in STEP_FIELDS:
            await state.set_state(ScriptForm.steps)
            emoji, title, about = STEP_FIELDS[field]
            steps = data.get("steps", [])
            text = (f"{emoji} <b>{title}</b>\n{about}\n\n{STEPS_HELP}\n\n"
                    f"<b>Шаги ({len(steps)}):</b>\n{pretty_steps(steps)}\n\n{PLACEHOLDERS}")
            extra = [("✅ Готово", "sc:st:done")]
            more = [("↩️ Убрать последний", "sc:st:undo"), ("🗑 Очистить", "sc:st:clear")] if steps else None
            kb = nav(data, extra, more)
        elif field == "check_cmd":
            await state.set_state(ScriptForm.check)
            text = ("🔎 <b>Проверка установки</b> (необязательно)\n\n"
                    "Команда, которая успешна, если скрипт <b>уже установлен</b>. Тогда бот не будет "
                    "запускать установку повторно.\n\n⚠️ <b>Обязательно задайте, если скрипт при повторном "
                    "запуске показывает меню</b> (как OpenVPN): иначе ответы установки попадут в меню.\n\n"
                    "Пример для OpenVPN:\n<pre>test -f /etc/openvpn/server.conf</pre>"
                    + (f"\nСейчас: <code>{esc(cur['check_cmd'])}</code>" if cur.get("check_cmd") else ""))
            kb = nav(data, [("Пропустить" if data.get("mode") == "wizard" else "Убрать проверку", "sc:c:skip")])
        else:
            await state.set_state(ScriptForm.result_type)
            text = ("📤 <b>Где взять ключ?</b>\n\n📄 <b>Файл на сервере</b> — скрипт создаёт файл "
                    "(например <code>.ovpn</code>), бот скачает его.\n💻 <b>Вывод команд</b> — ключ "
                    "печатается в терминал (например <code>vless://…</code>).")
            kb = nav(data, [("📄 Файл на сервере", "sc:r:file"), ("💻 Вывод команд", "sc:r:output")])
        text = header(data) + text
        await state.update_data(field=field)
        if isinstance(target, CallbackQuery):
            await edit(target, text, kb)
            await state.update_data(prompt_id=target.message.message_id)
            return
        prompt_id = data.get("prompt_id")
        if prompt_id:
            try:
                await target.bot.edit_message_text(text, chat_id=target.chat.id, message_id=prompt_id,
                                                   reply_markup=kb, disable_web_page_preview=True)
                return
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return
        msg = await target.answer(text, reply_markup=kb, disable_web_page_preview=True)
        await state.update_data(prompt_id=msg.message_id)

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
                if isinstance(target, Message):
                    await target.answer("✅ <b>Скрипт добавлен!</b>")
                return await show_card(target, db, script_id)
            return await ask(target, state, WIZARD[idx + 1])
        if values.get("content") is not None:
            values["content"] = bytes.fromhex(values["content"])
        db.update_script(data["script_id"], **values)
        await state.clear()
        if isinstance(target, Message):
            await target.answer("✅ Сохранено.")
        await show_card(target, db, data["script_id"])

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

    async def _delete(message: Message):
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    @r.message(ScriptForm.name, F.text)
    async def in_name(m: Message, state: FSMContext, db: DB):
        await _delete(m)
        await field_done(m, state, db, name=m.text.strip()[:40])

    def raw_github(url: str) -> str:
        mm = re.match(r"^https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url)
        return f"https://raw.githubusercontent.com/{mm[1]}/{mm[2]}/{mm[3]}" if mm else url

    def safe_name(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:64].strip("._") or "script.sh"

    @r.message(ScriptForm.file, F.document)
    async def in_file_doc(m: Message, state: FSMContext, db: DB):
        if m.document.file_size > MAX_FILE:
            return await m.reply("❌ Файл больше 20 МБ.")
        content = (await m.bot.download(m.document)).read()
        await field_done(m, state, db, filename=safe_name(m.document.file_name or "script.sh"),
                         content=content.hex(), source_url=None)

    @r.message(ScriptForm.file, F.text)
    async def in_file_url(m: Message, state: FSMContext, db: DB):
        url = raw_github(m.text.strip())
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return await m.reply("❌ Отправьте файл документом или ссылку http(s)://…")
        wait = await m.reply("⏳ Скачиваю…")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
                async with session.get(url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        raise ValueError(f"сервер ответил {resp.status}")
                    content = await resp.content.read(MAX_FILE + 1)
            if len(content) > MAX_FILE:
                raise ValueError("файл больше 20 МБ")
        except Exception as e:
            return await wait.edit_text(f"❌ Не удалось скачать: {esc(str(e))}")
        await _delete(wait)
        await field_done(m, state, db, filename=safe_name(parsed.path.rsplit("/", 1)[-1]),
                         content=content.hex(), source_url=url)

    @r.callback_query(ScriptForm.file, F.data == "sc:f:nofile")
    async def cb_nofile(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, filename=None, content=None, source_url=None)

    @r.message(ScriptForm.steps, F.text)
    async def in_steps(m: Message, state: FSMContext):
        new = parse_steps(m.text)
        await _delete(m)
        data = await state.get_data()
        await state.update_data(steps=data.get("steps", []) + new)
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
        await _delete(m)
        await field_done(m, state, db, check_cmd=m.text.strip())

    @r.callback_query(ScriptForm.check, F.data == "sc:c:skip")
    async def cb_check_skip(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, check_cmd="")

    @r.callback_query(ScriptForm.result_type, F.data.in_({"sc:r:file", "sc:r:output"}))
    async def cb_result_type(cb: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await cb.answer()
        if cb.data == "sc:r:file":
            await state.set_state(ScriptForm.result_path)
            await edit(cb, header(data) + "📄 <b>Путь к файлу ключа</b>\n\nГде скрипт сохраняет файл? "
                       "Используйте <code>{name}</code> вместо названия ключа.\n\n"
                       "Пример для OpenVPN:\n<pre>/root/{name}.ovpn</pre>", nav(data))
        else:
            await state.set_state(ScriptForm.result_regex)
            await edit(cb, header(data) + "🔎 <b>Фильтр ключа</b>\n\nРегулярное выражение — из вывода "
                       "возьмётся только совпадение. Например для VLESS:\n<pre>vless://\\S+</pre>\n"
                       "Или нажмите «Весь вывод».", nav(data, [("Весь вывод", "sc:r:all")]))

    @r.message(ScriptForm.result_path, F.text)
    async def in_result_path(m: Message, state: FSMContext, db: DB):
        path = m.text.strip()
        if "\n" in path or len(path) > 300:
            return await m.reply("❌ Нужен один путь, например <code>/root/{name}.ovpn</code>")
        await _delete(m)
        await field_done(m, state, db, result_type="file", result_path=path, key_regex="")

    @r.message(ScriptForm.result_regex, F.text)
    async def in_result_regex(m: Message, state: FSMContext, db: DB):
        try:
            re.compile(m.text.strip())
        except re.error as e:
            return await m.reply(f"❌ Некорректное выражение: {esc(str(e))}")
        await _delete(m)
        await field_done(m, state, db, result_type="output", key_regex=m.text.strip(), result_path="")

    @r.callback_query(ScriptForm.result_regex, F.data == "sc:r:all")
    async def cb_result_all(cb: CallbackQuery, state: FSMContext, db: DB):
        await cb.answer()
        await field_done(cb, state, db, result_type="output", key_regex="", result_path="")

    @r.message(ScriptForm.file)
    @r.message(ScriptForm.steps)
    @r.message(ScriptForm.name)
    async def in_wrong(m: Message):
        await m.reply("⚠️ Не тот формат. Следуйте подсказке выше.")

    return r
