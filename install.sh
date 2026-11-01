#!/bin/bash
# Установка бота как systemd-сервиса. Запускать от root в папке репозитория.
set -e
cd "$(dirname "$(readlink -f "$0")")"
DIR=$(pwd)

if [[ $EUID -ne 0 ]]; then echo "Запустите от root: sudo bash install.sh"; exit 1; fi
if [[ ! -e /etc/openvpn/server.conf ]]; then
	echo "OpenVPN не установлен. Сначала запустите: bash scripts/openvpn-install.sh"
	exit 1
fi

if command -v apt-get >/dev/null; then
	apt-get update -y && apt-get install -y python3 python3-venv python3-pip
elif command -v dnf >/dev/null; then
	dnf install -y python3 python3-pip
elif command -v yum >/dev/null; then
	yum install -y python3 python3-pip
fi

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [[ ! -f .env ]]; then
	cp .env.example .env
	read -rp "Токен бота от @BotFather: " TOKEN
	read -rp "Ваш Telegram ID (узнать у @userinfobot): " ADMIN
	read -rp "Контакт поддержки для клиентов (например @username): " SUPPORT
	sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$TOKEN|; s|^ADMIN_IDS=.*|ADMIN_IDS=$ADMIN|; s|^SUPPORT_CONTACT=.*|SUPPORT_CONTACT=$SUPPORT|" .env
	chmod 600 .env
fi

cat >/etc/systemd/system/openvpn-seller-bot.service <<UNIT
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
systemctl enable --now openvpn-seller-bot
sleep 2
systemctl --no-pager status openvpn-seller-bot | head -n 12
echo
echo "Готово! Напишите боту /start. Логи: journalctl -u openvpn-seller-bot -f"
