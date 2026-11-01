#!/bin/bash
# Меню управления ботом на сервере. Запуск: vpnbot
DIR="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SERVICE="openvpn-seller-bot"
cd "$DIR" || exit 1
[[ $EUID -eq 0 ]] || { echo "Запустите от root"; exit 1; }

pause() { read -n1 -r -s -p $'\nНажмите любую клавишу…' </dev/tty; }

while true; do
	clear
	if systemctl is-active -q "$SERVICE"; then
		STATUS=$'\e[32m● работает\e[0m'
	else
		STATUS=$'\e[31m● остановлен\e[0m'
	fi
	echo -e "\e[1;36m══════ OpenVPN Seller Bot ══════\e[0m"
	echo -e "  Бот: $STATUS"
	echo
	echo "  1) Показать логи (последние 50 строк)"
	echo "  2) Смотреть логи вживую (выход: Ctrl+C)"
	echo "  3) Перезапустить бота"
	echo "  4) Остановить бота"
	echo "  5) Обновить бота"
	echo "  6) Изменить настройки (токен, админ, поддержка)"
	echo "  7) Меню OpenVPN на этом сервере (скрипт)"
	echo "  8) Удалить бота"
	echo "  0) Выход"
	echo
	read -rp "Выберите: " C </dev/tty
	case $C in
	1) journalctl -u "$SERVICE" -n 50 --no-pager; pause ;;
	2) journalctl -u "$SERVICE" -f ;;
	3) systemctl restart "$SERVICE" && echo "Перезапущен"; pause ;;
	4) systemctl stop "$SERVICE" && echo "Остановлен"; pause ;;
	5) bash "$DIR/install.sh"; pause ;;
	6) bash "$DIR/install.sh" --reconfigure; pause ;;
	7)
		[[ -e /etc/openvpn/server.conf ]] || { echo "На этом сервере OpenVPN не установлен. Серверы добавляются в боте: 🖥 Серверы."; pause; continue; }
		echo "Совет: ключи удобнее создавать в боте. Созданные здесь ключи бот подхватит сам."
		bash "$DIR/scripts/openvpn-install.sh"
		systemctl restart "$SERVICE"
		pause ;;
	8)
		read -rp "Точно удалить бота? OpenVPN и ключи останутся. [y/N]: " Y </dev/tty
		if [[ $Y =~ ^[yYдД]$ ]]; then
			systemctl disable --now "$SERVICE"
			rm -f "/etc/systemd/system/$SERVICE.service" /usr/local/bin/vpnbot
			systemctl daemon-reload
			echo "Бот удалён. Папка $DIR (с базой bot.db) оставлена — удалите вручную, если не нужна."
			exit 0
		fi ;;
	0 | "") exit 0 ;;
	esac
done
