#!/usr/bin/env bash
# Safe hot backup of the database. Add to cron:  0 4 * * * /opt/scorebot/deploy/backup.sh
set -euo pipefail
mkdir -p /opt/scorebot/backups
sqlite3 /opt/scorebot/data/scorebot.db ".backup /opt/scorebot/backups/scorebot-$(date +%F).db"
find /opt/scorebot/backups -name 'scorebot-*.db' -mtime +14 -delete
