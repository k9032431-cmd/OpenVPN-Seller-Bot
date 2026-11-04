"""Шаги скрипта и «живой» терминал.

Бот открывает на сервере настоящий терминал (PTY) и «печатает» строки по одной — как человек:
каждая строка вводится, когда скрипт ждёт ответа. Поэтому интерактивные скрипты (с вопросами)
работают без переделок. Логика перенесена из бота VPNCreator.

Формат шагов (каждый с новой строки):
  • команда или ответ скрипту — вводится и нажимается Enter
  • enter — просто нажать Enter (оставить значение по умолчанию)
  • ? текст вопроса => ответ — ответ на вопрос, который появляется не всегда
  • # комментарий — пропускается
"""
from __future__ import annotations

import asyncio
import codecs
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

REMOTE_DIR = "vpnbot"                        # папка ~/vpnbot на сервере — туда грузятся скрипты
DIR_SH = '"$VPNB_DIR"'                        # она же в командах
PREP = 'VPNB_DIR="$HOME/vpnbot"; mkdir -p "$VPNB_DIR" && cd "$VPNB_DIR" || exit 97; '

READY = "VPNB_READY$"
ENTER_WORDS = {"enter", "{enter}", "<enter>", "⏎", "↵"}
RULE_RE = re.compile(r"^\s*\?\s*(.+?)\s*=>\s*(.*)$")
_ANY_KEY = re.compile(r"any key|любую клавишу|istalan|basyň", re.I)
_LETTER = re.compile(r"[^\W\d_]")
_ESC = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\\\)|\x1b[()][A-Za-z0-9]|\x1b[=>78DEHM]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Убирает цветные ANSI-коды и управляющие символы из вывода."""
    text = _ESC.sub("", text or "")
    text = "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in text.split("\n"))
    return _CTRL.sub("", text)


def parse_steps(text: str) -> list[str]:
    return [l.rstrip() for l in (text or "").splitlines() if l.strip() and not l.lstrip().startswith("#")]


def fill(template: str, values: dict[str, str]) -> str:
    """Подставляет {плейсхолдеры} без str.format, чтобы не ломать ${VAR} в bash."""
    for k, v in values.items():
        template = template.replace("{" + k + "}", v)
    return template


def steps_of(raw: str, values: dict[str, str]) -> list[str]:
    return [fill(line, values) for line in parse_steps(raw)]


def session_output(output: str) -> str:
    return "\n".join(l for l in output.splitlines() if not l.startswith(READY)).strip()


@dataclass
class SessionResult:
    ok: bool
    output: str
    error: str = ""

    def tail(self, limit: int = 1500) -> str:
        return session_output(self.output)[-limit:]


class _Stuck(Exception):
    def __init__(self, tail: str):
        super().__init__(tail)
        self.tail = tail


OnStep = Callable[[int, str], Awaitable[None]]


async def run_session(conn: asyncssh.SSHClientConnection, lines: list[str], timeout: int,
                      on_step: OnStep | None = None, idle: float = 1.5, stuck: float = 45.0) -> SessionResult:
    """Открывает терминал с bash и «печатает» строки по одной (вход под root).

    Строка отправляется, когда терминал ждёт ввода: вывод затих, а последняя строка экрана
    похожа на вопрос. Если команда завершилась с ошибкой — сессия прерывается (set -e).
    """
    cmd = (f"{PREP}exec env VPNB_DIR=\"$VPNB_DIR\" PS1={shlex.quote(READY + ' ')} PS2='' HISTFILE=/dev/null "
           f"DEBIAN_FRONTEND=noninteractive TERM=xterm bash --norc --noprofile -i")
    proc = await conn.create_process(cmd, term_type="xterm", term_size=(200, 50), encoding=None)

    raw: list[str] = []
    state = {"last": time.monotonic(), "closed": False}
    decoder = codecs.getincrementaldecoder("utf-8")("replace")

    async def reader():
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    break
                raw.append(decoder.decode(data))
                state["last"] = time.monotonic()
        except (asyncssh.Error, OSError):
            pass
        finally:
            state["closed"] = True

    task = asyncio.create_task(reader())
    deadline = time.monotonic() + timeout

    def text() -> str:
        return "".join(raw)

    def screen_tail() -> str:
        t = _ESC.sub("", text()[-2000:])
        if t.endswith("\n"):
            return ""
        return _CTRL.sub("", t.rsplit("\n", 1)[-1].rsplit("\r", 1)[-1]).strip()

    async def wait_input(max_wait: float | None = None) -> str | None:
        """Ждёт, пока терминал запросит ввод. None — процесс завершился."""
        started = time.monotonic()
        while True:
            if state["closed"]:
                return None
            now = time.monotonic()
            if now > deadline:
                raise asyncio.TimeoutError
            if max_wait is not None and now - started > max_wait:
                raise _Stuck(screen_tail())
            if now - state["last"] >= idle:
                tail = screen_tail()
                if tail and _LETTER.search(tail):
                    return tail
            await asyncio.sleep(0.2)

    def result(ok: bool, error: str = "") -> SessionResult:
        return SessionResult(ok, clean(text()), error)

    # «? вопрос => ответ» — срабатывает, только если такой вопрос появился
    rules: list[tuple[str, str]] = []
    seq: list[str] = []
    for line in lines:
        if m := RULE_RE.match(line):
            rules.append((m[1].strip().lower(), m[2].strip()))
        else:
            seq.append(line)

    def rule_for(tail: str) -> str | None:
        if tail.startswith(READY):
            return None
        low = tail.lower()
        return next((answer for question, answer in rules if question in low), None)

    asked: dict = {}

    def repeated(tail: str) -> str | None:
        """Скрипт не принял ответ и спросил то же снова → стоп, чтобы ответы не съехали."""
        if not asked or tail.startswith(READY) or tail != asked["tail"]:
            return None
        if "\n" not in text()[asked["pos"]:]:
            return None
        return (f"Скрипт не принял ответ «{asked['line']}» на вопрос «{tail}» и спросил снова. "
                "Остановлено, чтобы не ввести лишнего. Возможно, скрипт уже установлен "
                "(задайте «Проверку установки») или вопросы идут в другом порядке")

    def fresh(tail: str) -> str:
        """Только то, что появилось на строке после предыдущего ответа. Иначе после
        «Press any key...v» следующий вопрос на той же строке снова считался бы «любой клавишей»
        и вместо «admin» ушла бы одна буква «a»."""
        if not asked:
            return tail
        since = _ESC.sub("", text()[asked["pos"]:])
        return since if "\n" not in since else tail

    def send(tail: str, line: str) -> None:
        nonlocal asked
        any_key = bool(_ANY_KEY.search(fresh(tail)))
        asked = {"tail": tail, "line": line, "pos": len(text())}
        if line.strip().lower() in ENTER_WORDS or not line.strip():
            proc.stdin.write(b"\r")
        elif any_key:
            proc.stdin.write(line.strip()[:1].encode() or b"\r")
        else:  # Ctrl+U стирает подставленное значение по умолчанию (read -e -i ...)
            proc.stdin.write(b"\x15" + line.encode() + b"\r")
        state["last"] = time.monotonic()

    try:
        if await wait_input(max_wait=30) is None:
            return result(False, "Терминал закрылся сразу после запуска")
        proc.stdin.write(b"set -e; bind 'set enable-bracketed-paste off' 2>/dev/null; clear\r")

        i = 0
        while i < len(seq):
            line = seq[i]
            tail = await wait_input()
            if tail is None:
                return result(False, f"Сессия завершилась на шаге {i + 1}: «{line}» — предыдущая команда упала")
            if err := repeated(tail):
                return result(False, err)
            if (answer := rule_for(tail)) is not None:
                if on_step:
                    await on_step(i + 1, f"{answer or 'Enter'} (на «{tail[:40]}»)")
                send(tail, answer)
                continue
            if on_step:
                await on_step(i + 1, line)
            send(tail, line)
            i += 1

        # все строки введены — ждём возврата в shell
        while True:
            try:
                tail = await wait_input(max_wait=stuck)
            except _Stuck as exc:
                return result(False, f"Скрипт ждёт ввода, а шаги закончились: «{exc.tail}»")
            if tail is None:
                return result(False, "Сессия завершилась с ошибкой")
            if tail.startswith(READY):
                break
            if err := repeated(tail):
                return result(False, err)
            if (answer := rule_for(tail)) is not None:
                send(tail, answer)
                continue
            started = time.monotonic()
            while not state["closed"] and screen_tail() == tail and time.monotonic() - started < stuck:
                await asyncio.sleep(0.5)
            if not state["closed"] and screen_tail() == tail:
                return result(False, f"Скрипт ждёт ввода, а шаги закончились: «{tail}»")
        proc.stdin.write(b"exit\r")
        try:
            await asyncio.wait_for(task, 10)
        except asyncio.TimeoutError:
            pass
        return result(True)
    except asyncio.TimeoutError:
        return result(False, f"Не уложились в {timeout} сек.")
    finally:
        if not state["closed"]:
            proc.close()
        task.cancel()
