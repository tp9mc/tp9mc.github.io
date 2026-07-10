#!/bin/bash
# uninstall.sh — удаляет MacScrub: приложение, ярлык, расписание, служебные данные.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Удаление MacScrub"

# 1) Расписание launchd
PLIST="$HOME/Library/LaunchAgents/com.macscrub.schedule.plist"
if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "  - расписание удалено"
fi

# 2) Приложение и ярлык
rm -rf "$ROOT/dist/MacScrub.app"
rm -f "$HOME/Desktop/MacScrub" 2>/dev/null || true
# Finder-алиас на рабочем столе
osascript >/dev/null 2>&1 <<'OSA' || true
tell application "Finder"
  try
    delete (every item of (desktop) whose name starts with "MacScrub")
  end try
end tell
OSA
echo "  - приложение и ярлык удалены"

# 3) Служебные данные и отчёты (спрашиваем)
SUPPORT="$HOME/Library/Application Support/MacScrub"
if [[ -d "$SUPPORT" ]]; then
  read -r -p "Удалить отчёты и настройки ($SUPPORT)? [y/N] " ans
  if [[ "$ans" == [yY]* ]]; then
    rm -rf "$SUPPORT"
    echo "  - служебные данные удалены"
  else
    echo "  - служебные данные оставлены"
  fi
fi

echo "==> Готово."
