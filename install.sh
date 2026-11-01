#!/bin/bash
# =====================================================================
#  OpenVPN Seller Bot — установка одной командой:
#
#    bash <(curl -sL https://raw.githubusercontent.com/k9032431-cmd/OpenVPN-Seller-Bot/HEAD/install.sh)
#
#  Что делает:
#    1. ставит нужные программы (git, python3)
#    2. скачивает бота в /opt/openvpn-seller-bot
#    3. ставит OpenVPN, если его ещё нет
#    4. спрашивает токен бота и сам узнаёт ваш Telegram ID
#    5. запускает бота как сервис (сам стартует после перезагрузки)
#
#  Повторный запуск = обновление (настройки сохраняются).
#  Изменить настройки:  vpnbot   (или: bash install.sh --reconfigure)
# =====================================================================
set -e

REPO="https://github.com/k9032431-cmd/OpenVPN-Seller-Bot.git"
DIR="/opt/openvpn-seller-bot"
SERVICE="openvpn-seller-bot"
RECONFIGURE=0
[[ $1 == "--reconfigure" ]] && RECONFIGURE=1

# ---------- оформление ----------
step() { echo; echo -e "\e[1;36m━━━ $* ━━━\e[0m"; }
ok()   { echo -e "\e[32m✔ $*\e[0m"; }
warn() { echo -e "\e[33m! $*\e[0m"; }
die()  { echo -e "\e[31m✘ $*\e[0m"; exit 1; }
ask()  { local a; read -rp "$1" a </dev/tty; echo "$a"; }   # работает и через curl | bash

[[ $EUID -eq 0 ]] || die "Запустите от root (sudo -i, потом команду ещё раз)."

# Если скрипт запущен из скачанной папки — используем её
SRC="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
[[ -f "$SRC/bot/__main__.py" ]] && DIR="$SRC"

# ---------- 1. программы ----------
step "1/5  Устанавливаю нужные программы"
if command -v apt-get >/dev/null; then
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq git curl ca-certificates python3 python3-venv python3-pip >/dev/null
elif command -v dnf >/dev/null; then
	dnf install -y -q git curl python3 python3-pip
elif command -v yum >/dev/null; then
	yum install -y -q git curl python3 python3-pip
else
	die "Неизвестная система. Нужна Ubuntu/Debian/CentOS."
fi
ok "Готово"

# ---------- 2. код бота ----------
step "2/5  Скачиваю бота"
if [[ -d "$DIR/.git" ]]; then
	git -C "$DIR" pull -q --ff-only || warn "Не удалось обновить код, продолжаю с текущим"
elif [[ ! -f "$DIR/bot/__main__.py" ]]; then
	git clone -q "$REPO" "$DIR" || die "Не удалось скачать $REPO (репозиторий должен быть публичным)"
fi
cd "$DIR"
chmod +x install.sh vpnbot.sh scripts/openvpn-install.sh
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip >/dev/null
.venv/bin/pip install -q -r requirements.txt
ln -sf "$DIR/vpnbot.sh" /usr/local/bin/vpnbot
ok "Бот в папке $DIR"

# ---------- 3. OpenVPN ----------
step "3/5  OpenVPN"
if [[ -e /etc/openvpn/server.conf ]]; then
	ok "OpenVPN уже установлен"
else
	echo "OpenVPN ещё не установлен."
	echo "  1) Установить автоматически (рекомендуется: порт 1194, UDP, AdGuard DNS)"
	echo "  2) Установить вручную — ответить на вопросы скрипта"
	CHOICE=$(ask "Выберите [1-2, по умолчанию 1]: ")
	if [[ $CHOICE == "2" ]]; then
		bash scripts/openvpn-install.sh </dev/tty || true
	else
		PUBLIC_IP=$(curl -4 -s --max-time 10 https://api.ipify.org || true)
		AUTO_INSTALL=y DNS=11 CLIENT=test ENDPOINT="$PUBLIC_IP" \
			bash scripts/openvpn-install.sh </dev/tty || true
	fi
	[[ -e /etc/openvpn/server.conf ]] || die "OpenVPN не установился, смотрите ошибки выше."
	ok "OpenVPN установлен"
fi

# ---------- 4. настройки ----------
step "4/5  Настройки бота"
tg() { curl -s --max-time 30 "https://api.telegram.org/bot$TOKEN/$1"; }
json() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)" 2>/dev/null || true; }

if [[ -f .env ]] && grep -q '^BOT_TOKEN=.\+' .env && [[ $RECONFIGURE == 0 ]]; then
	ok "Настройки уже есть (.env). Изменить: vpnbot → «Изменить настройки»"
else
	systemctl stop "$SERVICE" 2>/dev/null || true   # чтобы бот не «съел» сообщение ниже

	echo "Создайте бота: откройте @BotFather в Telegram → /newbot → придумайте имя."
	echo "BotFather пришлёт токен вида 123456789:AAH...."
	while true; do
		TOKEN=$(ask "Вставьте токен: " | tr -d ' ')
		BOT=$(tg getMe | json 'd["result"]["username"]')
		[[ -n $BOT ]] && break
		warn "Токен не подошёл, попробуйте ещё раз."
	done
	ok "Бот найден: @$BOT"

	echo
	echo -e "Теперь откройте \e[1mhttps://t.me/$BOT\e[0m и нажмите «Start» (или напишите любое сообщение)."
	echo "Так я узнаю ваш Telegram ID и сделаю вас администратором. Жду до 3 минут…"
	tg deleteWebhook >/dev/null
	ADMIN=""; OFFSET=0
	for _ in $(seq 1 18); do
		UPD=$(tg "getUpdates?timeout=10&offset=$OFFSET")
		ADMIN=$(echo "$UPD" | json '[u["message"]["from"]["id"] for u in d["result"] if "message" in u][-1]')
		if [[ -n $ADMIN ]]; then
			ADMIN_NAME=$(echo "$UPD" | json '[u["message"]["from"].get("first_name","") for u in d["result"] if "message" in u][-1]')
			ADMIN_USER=$(echo "$UPD" | json '[u["message"]["from"].get("username","") for u in d["result"] if "message" in u][-1]')
			LAST=$(echo "$UPD" | json 'd["result"][-1]["update_id"]')
			tg "getUpdates?offset=$((LAST + 1))" >/dev/null
			break
		fi
	done
	if [[ -n $ADMIN ]]; then
		ok "Администратор: $ADMIN_NAME (ID $ADMIN)"
	else
		warn "Сообщение не пришло. Узнайте свой ID у @userinfobot."
		ADMIN=$(ask "Ваш Telegram ID: ")
	fi

	DEFAULT_SUPPORT=""
	[[ -n $ADMIN_USER ]] && DEFAULT_SUPPORT="@$ADMIN_USER"
	SUPPORT=$(ask "Контакт поддержки для клиентов [Enter = ${DEFAULT_SUPPORT:-пусто}]: ")
	SUPPORT=${SUPPORT:-$DEFAULT_SUPPORT}

	cat >.env <<EOF
# Настройки бота. После изменения: vpnbot → «Перезапустить»
BOT_TOKEN=$TOKEN
# Администраторы (Telegram ID через запятую)
ADMIN_IDS=$ADMIN
SUPPORT_CONTACT=$SUPPORT
OPENVPN_DIR=/etc/openvpn
EASYRSA_DIR=/etc/openvpn/easy-rsa
# Следить за сменой IP и рассылать клиентам новые ключи
IP_AUTO_UPDATE=true
IP_CHECK_INTERVAL=60
IP_CONFIRM_CHECKS=2
# За сколько дней предупреждать клиента об окончании ключа
WARN_DAYS=3
EOF
	chmod 600 .env
	ok "Настройки сохранены"
fi

# ---------- 5. запуск ----------
step "5/5  Запускаю бота"
cat >/etc/systemd/system/$SERVICE.service <<UNIT
[Unit]
Description=OpenVPN Seller Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m bot
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 3

if systemctl is-active -q "$SERVICE"; then
	BOT=${BOT:-$(TOKEN=$(grep '^BOT_TOKEN=' .env | cut -d= -f2-); tg getMe | json 'd["result"]["username"]')}
	echo
	echo -e "\e[1;32m╔══════════════════════════════════════════════╗"
	echo -e "║            ✅  ВСЁ ГОТОВО!                    ║"
	echo -e "╚══════════════════════════════════════════════╝\e[0m"
	echo
	echo -e "  Откройте бота: \e[1mhttps://t.me/$BOT\e[0m  → нажмите /start"
	echo "  Там кнопки: «➕ Новый ключ», «📋 Ключи», «❓ Как это работает»"
	echo
	echo -e "  Управление на сервере: \e[1mvpnbot\e[0m  (статус, логи, обновление, настройки)"
	echo
else
	journalctl -u "$SERVICE" -n 30 --no-pager
	die "Бот не запустился — ошибка выше. Исправить настройки: vpnbot"
fi
