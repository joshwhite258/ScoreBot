#!/usr/bin/env bash
# ScoreBot installer for a Debian 12 LXC. Run as root from the unzipped scorebot folder:
#   bash deploy/install.sh
set -euo pipefail

APP=/opt/scorebot
SRC="$(cd "$(dirname "$0")/.." && pwd)"

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo bash deploy/install.sh)"; exit 1; }

echo "==> Installing system packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip sqlite3 tzdata ca-certificates >/dev/null

echo "==> Creating scorebot user"
id scorebot &>/dev/null || useradd --system --home "$APP" --shell /usr/sbin/nologin scorebot

echo "==> Copying app to $APP"
mkdir -p "$APP/data"
cp -r "$SRC/scorebot" "$SRC/deploy" "$SRC/requirements.txt" "$APP/"
[[ -f "$APP/.env" ]] || cp "$SRC/.env.example" "$APP/.env"
chmod 600 "$APP/.env"

echo "==> Python virtualenv + dependencies"
[[ -d "$APP/venv" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

chown -R scorebot:scorebot "$APP"

echo "==> systemd service"
cp "$SRC/deploy/scorebot.service" /etc/systemd/system/scorebot.service
# /scorebot update from Discord: a root path unit watches for the bot's request file.
sed "s|@SRC@|$SRC|g" "$SRC/deploy/scorebot-update.service" > /etc/systemd/system/scorebot-update.service
cp "$SRC/deploy/scorebot-update.path" /etc/systemd/system/scorebot-update.path
systemctl daemon-reload
systemctl enable scorebot >/dev/null
systemctl enable --now scorebot-update.path >/dev/null

if grep -q "paste-your" "$APP/.env"; then
  echo
  echo "Almost done! Add your keys:   nano $APP/.env"
  echo "Then start it:                systemctl start scorebot"
  echo "Watch the logs:               journalctl -u scorebot -f"
else
  systemctl restart scorebot
  echo "ScoreBot (re)started. Logs: journalctl -u scorebot -f"
fi
