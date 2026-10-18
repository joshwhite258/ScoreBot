#!/usr/bin/env bash
# Pull the latest ScoreBot from GitHub and reinstall/restart, but only if there's a new commit.
# Meant for cron, e.g. daily at 4:30am:
#   30 4 * * * bash /root/scorebot/deploy/auto-update.sh >> /var/log/scorebot-update.log 2>&1
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

git fetch -q origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse '@{u}')

if [[ "$LOCAL" == "$REMOTE" ]]; then
  echo "$(date '+%F %T') up to date (${LOCAL:0:7})"
  exit 0
fi

echo "$(date '+%F %T') updating ${LOCAL:0:7} -> ${REMOTE:0:7}"
git pull -q --ff-only
bash deploy/install.sh
echo "$(date '+%F %T') done"
