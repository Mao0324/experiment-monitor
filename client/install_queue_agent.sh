#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "请使用 sudo bash install_queue_agent.sh"
  exit 1
fi

SOURCE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -o root -g root -m 0755 /opt/yolo-monitor-agent
install -o root -g root -m 0755 "${SOURCE_DIR}/queue_agent.py" /opt/yolo-monitor-agent/queue_agent.py
install -o root -g root -m 0644 "${SOURCE_DIR}/yolo-monitor-agent.service" /etc/systemd/system/yolo-monitor-agent.service
if [[ ! -f /etc/yolo-monitor-agent.json ]]; then
  install -o root -g root -m 0600 "${SOURCE_DIR}/queue_agent_config.example.json" /etc/yolo-monitor-agent.json
  echo "已创建 /etc/yolo-monitor-agent.json，请填写 API token 并确认训练目录。"
else
  echo "保留现有 /etc/yolo-monitor-agent.json。"
fi
systemctl daemon-reload
systemctl enable yolo-monitor-agent
echo "配置完成后启动：sudo systemctl restart yolo-monitor-agent"
echo "查看日志：journalctl -u yolo-monitor-agent -f"
