#!/usr/bin/env bash
# Instalasi apisms di VPS Ubuntu/Debian.
# Dengan domain (nginx + HTTPS Let's Encrypt):
#   chmod +x install.sh && sudo ./install.sh apisms.domainkamu.com
# Tanpa domain (pakai IP VPS langsung, API di http://IP_VPS:2020):
#   chmod +x install.sh && sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Jalankan sebagai root: sudo ./install.sh [domain]"
  exit 1
fi

DOMAIN="${1:-}"
APP_NAME="apisms"
APP_DIR="/var/www/${APP_NAME}"
APP_USER="apisms"
PORT="2020"
# Tanpa domain → bind 0.0.0.0 agar API & webhook bisa diakses via http://IP_VPS:PORT.
BIND_HOST="127.0.0.1"
if [ -z "${DOMAIN}" ]; then
  BIND_HOST="0.0.0.0"
fi
SERVICE_FILE="/etc/systemd/system/${APP_NAME}.service"
NGINX_CONF="/etc/nginx/sites-available/${APP_NAME}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> [1/7] install paket sistem"
apt-get update -y
if [ -n "${DOMAIN}" ]; then
  apt-get install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx curl
else
  apt-get install -y python3 python3-venv python3-pip curl
fi

echo "==> [2/7] siapkan user & folder ${APP_DIR}"
id -u "${APP_USER}" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "${APP_USER}"
mkdir -p "${APP_DIR}"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  "${SRC_DIR}/" "${APP_DIR}/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}"

echo "==> [3/7] virtualenv & dependensi"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

echo "==> [4/7] cek konfigurasi"
if [ ! -s "${APP_DIR}/data/providers.json" ]; then
  echo "!! data/providers.json kosong. Isi api_key provider & api_key klien dulu."
  exit 1
fi
for f in service.json country.json; do
  [ -s "${APP_DIR}/data/${f}" ] || { echo "!! data/${f} tidak ada."; exit 1; }
done

echo "==> [5/7] systemd service ${APP_NAME}"
cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=apisms - API order & check SMS
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/hypercorn main:app --bind ${BIND_HOST}:${PORT} --workers 2 --keep-alive 120 --access-logfile -
Restart=always
RestartSec=3
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${APP_NAME}"
systemctl restart "${APP_NAME}"

if [ -n "${DOMAIN}" ]; then
  echo "==> [6/7] nginx reverse proxy untuk ${DOMAIN}"
  cat > "${NGINX_CONF}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
    }
}
EOF

  ln -sf "${NGINX_CONF}" "/etc/nginx/sites-enabled/${APP_NAME}"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx

  echo "==> [7/7] HTTPS (Let's Encrypt)"
  certbot --nginx -d "${DOMAIN}" --redirect --non-interactive --agree-tos --register-unsafely-without-email 2>/tmp/certbot.log || {
    echo "!! certbot gagal. Lihat /tmp/certbot.log. Pastikan DNS ${DOMAIN} sudah mengarah ke IP server, lalu jalankan:"
    echo "   certbot --nginx -d ${DOMAIN} --redirect"
  }
else
  echo "==> [6/7] dilewati (tanpa domain)"
  echo "==> [7/7] dilewati (tanpa domain)"
fi

# Firewall (ufw) dibuka otomatis bila ufw aktif; security group cloud tetap lewat console.
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  if [ -n "${DOMAIN}" ]; then
    ufw allow 80/tcp
    ufw allow 443/tcp
  else
    ufw allow "${PORT}/tcp"
  fi
fi

echo
echo "==> selesai"
# Tunggu uvicorn selesai bind port (start systemd butuh beberapa detik; curl tunggal
# kerap kena "Connection refused" padahal service benar-benar hidup).
up=0
for _ in $(seq 1 15); do
  if curl -fsS "http://127.0.0.1:${PORT}/" >/dev/null 2>&1; then
    up=1
    break
  fi
  sleep 1
done
if [ "${up}" = "1" ]; then
  curl -fsS "http://127.0.0.1:${PORT}/" && echo
else
  echo "!! service belum merespons setelah 15 detik. Cek:"
  echo "   systemctl status ${APP_NAME}"
  echo "   journalctl -u ${APP_NAME} -n 50"
fi
if [ -n "${DOMAIN}" ]; then
  BASE_URL="https://${DOMAIN}"
else
  IP="$(curl -fsS4 --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  BASE_URL="http://${IP:-IP_VPS}:${PORT}"
fi
echo "Endpoint:"
echo "  POST ${BASE_URL}/order-sms"
echo "  POST ${BASE_URL}/check-sms"
echo "  POST ${BASE_URL}/check-country"
if [ -z "${DOMAIN}" ]; then
  echo
  echo "Webhook (daftarkan di dashboard provider):"
  echo "  ${BASE_URL}/webhook/grizzly_sms"
  echo "  ${BASE_URL}/webhook/sms_bower"
  echo "  ${BASE_URL}/webhook/hero_sms"
fi
echo
echo "Perintah berguna:"
echo "  systemctl status ${APP_NAME}"
echo "  journalctl -u ${APP_NAME} -f        # lihat log [ORDER]/[CHECK]/[COUNTRY]/[REDIS]"
echo "  systemctl restart ${APP_NAME}"
