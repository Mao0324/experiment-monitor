#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run with sudo: sudo bash install.sh"
  exit 1
fi

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
APP_ROOT=/opt/experiment-monitor
APP_DIR=${APP_ROOT}/app
DATA_DIR=${APP_ROOT}/data
DOWNLOAD_DIR=${APP_ROOT}/downloads
ENV_FILE=/etc/experiment-monitor.env

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y python3 nginx certbot python3-certbot-nginx openssl

if ! id experiment-monitor >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_ROOT}" --shell /usr/sbin/nologin experiment-monitor
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o experiment-monitor -g experiment-monitor -m 0750 "${DATA_DIR}"
install -d -o experiment-monitor -g experiment-monitor -m 0750 "${DATA_DIR}/backups"
install -d -o root -g root -m 0755 "${DOWNLOAD_DIR}"
install -o root -g root -m 0644 "${SOURCE_DIR}/monitor_server.py" "${APP_DIR}/monitor_server.py"
install -o root -g root -m 0644 "${SOURCE_DIR}/monitor_maintenance.py" "${APP_DIR}/monitor_maintenance.py"
install -o root -g root -m 0644 "${SOURCE_DIR}/experiment-monitor.service" /etc/systemd/system/experiment-monitor.service
install -o root -g root -m 0644 "${SOURCE_DIR}/experiment-monitor-backup.service" /etc/systemd/system/experiment-monitor-backup.service
install -o root -g root -m 0644 "${SOURCE_DIR}/experiment-monitor-backup.timer" /etc/systemd/system/experiment-monitor-backup.timer

if [[ ! -f "${ENV_FILE}" ]]; then
  install -o root -g root -m 0600 "${SOURCE_DIR}/experiment-monitor.env.example" "${ENV_FILE}"
  api_token=$(openssl rand -hex 32)
  session_secret=$(openssl rand -hex 32)
  sed -i "s/replace-with-a-random-64-character-token/${api_token}/" "${ENV_FILE}"
  sed -i "s/replace-with-another-random-64-character-secret/${session_secret}/" "${ENV_FILE}"
  echo "Created ${ENV_FILE} with random API/session secrets."
else
  echo "Preserved existing ${ENV_FILE}."
fi

systemctl daemon-reload
systemctl enable experiment-monitor
systemctl enable --now experiment-monitor-backup.timer

echo
echo "Application files installed. Next steps:"
echo "  1. Edit ${ENV_FILE} and replace every remaining placeholder."
echo "  2. Configure Nginx and HTTPS as described in DEPLOY.md."
echo "  3. Start with: systemctl restart experiment-monitor"
