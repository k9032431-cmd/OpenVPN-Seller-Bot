"""Готовые скрипты, которые админ может добавить одной кнопкой (потом их можно править)."""
from __future__ import annotations

from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

# Ответы на вопросы scripts/openvpn-install.sh по порядку (как в боте VPNCreator)
OPENVPN_INSTALL = """\
chmod +x vpn
./vpn
? Public IPv4 address => {server_ip}
enter
n
2
443
1
9
n
y
3
2
2
2
2
2
2
2
v
admin
enter"""

OPENVPN_KEY = """\
./vpn
1
{name}
enter"""

OPENVPN_DELETE = (
    "cd /etc/openvpn/easy-rsa && ./easyrsa --batch revoke {name} && EASYRSA_CRL_DAYS=3650 ./easyrsa gen-crl"
    " && cp pki/crl.pem /etc/openvpn/crl.pem && chmod 644 /etc/openvpn/crl.pem && rm -f /root/{name}.ovpn"
)

OPENVPN_IP = (
    "sed -i 's/{old_ip_re}/{new_ip}/g' /etc/openvpn/client-template.txt\n"
    "sed -i 's/{old_ip_re}/{new_ip}/g' /root/*.ovpn 2>/dev/null || true"
)


def openvpn() -> dict:
    return {
        "name": "OpenVPN",
        "filename": "vpn",
        "content": (SCRIPTS_DIR / "openvpn-install.sh").read_bytes(),
        "install_steps": OPENVPN_INSTALL,
        "check_cmd": "test -f /etc/openvpn/server.conf",
        "key_steps": OPENVPN_KEY,
        "result_type": "file",
        "result_path": "/root/{name}.ovpn",
        "delete_steps": OPENVPN_DELETE,
        "ip_steps": OPENVPN_IP,
    }
