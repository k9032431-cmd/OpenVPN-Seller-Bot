"""Настройки бота из переменных окружения / файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "y", "on", "da", "да")


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    support_contact: str
    data_dir: str
    db_path: str
    ip_auto_update: bool
    ip_check_interval: int
    ip_confirm_checks: int
    warn_days: int


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан. Запустите на сервере: vpnbot → «Изменить настройки».")

    admins = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
    )
    if not admins:
        raise SystemExit("ADMIN_IDS не задан. Укажите свой Telegram ID в .env.")

    data_dir = os.getenv("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    return Config(
        bot_token=token,
        admin_ids=admins,
        support_contact=os.getenv("SUPPORT_CONTACT", "").strip(),
        data_dir=data_dir,
        db_path=os.getenv("DB_PATH", str(Path(__file__).resolve().parent.parent / "bot.db")),
        ip_auto_update=_bool("IP_AUTO_UPDATE", True),
        ip_check_interval=max(15, _int("IP_CHECK_INTERVAL", 60)),
        ip_confirm_checks=max(1, _int("IP_CONFIRM_CHECKS", 2)),
        warn_days=max(0, _int("WARN_DAYS", 3)),
    )
