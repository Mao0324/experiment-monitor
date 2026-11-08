#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

COMMIT=${1:?Usage: sudo bash update-monitor-server.sh GIT_COMMIT}
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ ]] || { echo 'Expected a full Git commit SHA' >&2; exit 1; }
APP=/opt/experiment-monitor/app
SERVICE=experiment-monitor
REPO=https://codeload.github.com/Mao0324/experiment-monitor/tar.gz

if [[ $EUID -ne 0 ]]; then
  echo 'Run this script with sudo' >&2
  exit 1
fi
test -f "$APP/monitor_server.py"
command -v curl >/dev/null
command -v python3 >/dev/null
PID=$(systemctl show "$SERVICE" -p MainPID --value)
test "${PID:-0}" -gt 0

readarray -t CONFIG < <(python3 - "$PID" <<'PY'
import sys
from pathlib import Path

env = dict(item.split(b'=', 1) for item in Path('/proc/' + sys.argv[1] + '/environ').read_bytes().split(b'\0') if b'=' in item)
for key, default in [('MONITOR_DB', '/opt/experiment-monitor/data/monitor.db'), ('MONITOR_DOWNLOAD_DIR', '/opt/experiment-monitor/downloads'), ('MONITOR_PORT', '8765')]:
    print(env.get(key.encode(), default.encode()).decode())
PY
)
test "${#CONFIG[@]}" -eq 3
DB=${CONFIG[0]}
DOWNLOADS=${CONFIG[1]}
PORT=${CONFIG[2]}
[[ "$PORT" =~ ^[0-9]+$ ]]

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/opt/experiment-monitor/backups/deploy-$STAMP"
mkdir -p "$BACKUP/source" "$DOWNLOADS"
chmod 700 "$BACKUP"
echo "Fetching GitHub commit $COMMIT"
curl --fail --location --retry 2 --connect-timeout 15 --max-time 180 \
  "$REPO/$COMMIT" -o "$BACKUP/source.tar.gz"
tar -xzf "$BACKUP/source.tar.gz" --strip-components=1 -C "$BACKUP/source"
SOURCE="$BACKUP/source"
python3 -m py_compile "$SOURCE/server/monitor_server.py" "$SOURCE/server/monitor_maintenance.py" "$SOURCE/client/queue_agent.py"

python3 - "$SOURCE" "$BACKUP/new-agent.zip" <<'PY'
import sys
import zipfile
from pathlib import Path

source, target = Path(sys.argv[1]), Path(sys.argv[2])
names = ['queue_agent.py', 'install_queue_agent.sh', 'yolo-monitor-agent.service',
         'queue_agent_config.example.json', 'test_profiles_ubuntu.example.json',
         'yolo_monitor.py', 'monitored_training.py',
         'monitored_target_saliency_trainer.py', 'train_dronevehicle_ddp_fixed.py',
         'monitor_config.example.env']
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
    for name in names:
        archive.write(source / 'client' / name, name)
    for name in ('QUEUE_AGENT.md', 'TEST_RUNS.md'):
        archive.write(source / name, name)
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
PY

cp -a "$APP" "$BACKUP/app"
if [[ -f "$DOWNLOADS/yolo-queue-agent.zip" ]]; then
  cp -a "$DOWNLOADS/yolo-queue-agent.zip" "$BACKUP/old-agent.zip"
fi
python3 - "$DB" "$BACKUP/monitor.db" <<'PY'
import sqlite3
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit('Configured database does not exist; stopping before deployment')
with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30) as source:
    with sqlite3.connect(sys.argv[2]) as target:
        source.backup(target)
PY

rollback() {
  trap - ERR
  echo "Deployment failed; restoring application files. Backup: $BACKUP" >&2
  cp -a "$BACKUP/app/." "$APP/"
  if [[ -f "$BACKUP/old-agent.zip" ]]; then
    cp -a "$BACKUP/old-agent.zip" "$DOWNLOADS/yolo-queue-agent.zip"
  fi
  systemctl restart "$SERVICE" || true
  journalctl -u "$SERVICE" -n 15 --no-pager >&2 || true
  exit 1
}
trap rollback ERR

for NAME in monitor_server.py monitor_maintenance.py; do
  install -m 0644 "$SOURCE/server/$NAME" "$APP/.$NAME.new"
  chown --reference="$APP/monitor_server.py" "$APP/.$NAME.new"
  mv -f "$APP/.$NAME.new" "$APP/$NAME"
done
install -m 0644 "$BACKUP/new-agent.zip" "$DOWNLOADS/.yolo-queue-agent.zip.new"
mv -f "$DOWNLOADS/.yolo-queue-agent.zip.new" "$DOWNLOADS/yolo-queue-agent.zip"

systemctl restart "$SERVICE"
HEALTHY=0
for ATTEMPT in {1..45}; do
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" \
      | python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ok") is True else 1)' 2>/dev/null; then
    HEALTHY=1
    break
  fi
  sleep 1
done
[[ "$HEALTHY" -eq 1 ]]
systemctl is-active --quiet "$SERVICE"
cmp "$SOURCE/server/monitor_server.py" "$APP/monitor_server.py"
cmp "$BACKUP/new-agent.zip" "$DOWNLOADS/yolo-queue-agent.zip"
trap - ERR

printf '\nDEPLOY_OK\nCommit: %s\nBackup: %s\n' "$COMMIT" "$BACKUP"
sha256sum "$APP/monitor_server.py" "$DOWNLOADS/yolo-queue-agent.zip"
echo 'Training agents were not restarted. Existing configuration and experiment records were retained.'
