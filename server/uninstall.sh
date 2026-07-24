#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Please run with sudo: sudo bash uninstall.sh"
  exit 1
fi

systemctl disable --now experiment-monitor 2>/dev/null || true
systemctl disable --now experiment-monitor-backup.timer 2>/dev/null || true
rm -f /etc/systemd/system/experiment-monitor.service
rm -f /etc/systemd/system/experiment-monitor-backup.service
rm -f /etc/systemd/system/experiment-monitor-backup.timer
systemctl daemon-reload

echo "Service removed. Data and configuration were intentionally preserved:"
echo "  /opt/experiment-monitor"
echo "  /etc/experiment-monitor.env"
echo "  /etc/nginx/sites-available/experiment-monitor"
