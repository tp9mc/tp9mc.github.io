#!/bin/bash
# build.sh — собирает MacScrub.app из исходников и кладёт ярлык на рабочий стол.
# Запускать НА macOS. Зависимостей нет: osacompile/sips/iconutil входят в систему.
#
#   ./scripts/build.sh [каталог_назначения]
#
# По умолчанию приложение собирается в <проект>/dist/MacScrub.app.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEST_DIR="${1:-$ROOT/dist}"
APP="$DEST_DIR/MacScrub.app"

echo "==> Сборка MacScrub.app"
if [[ "$(uname)" != "Darwin" ]]; then
  echo "ВНИМАНИЕ: это не macOS. Сборка .app возможна только на Mac (нужен osacompile)." >&2
  echo "Скрипт всё равно создаст структуру, но приложение запустится только на macOS." >&2
fi

mkdir -p "$DEST_DIR"
rm -rf "$APP"

# 1) Компилируем AppleScript в .app
if command -v osacompile >/dev/null 2>&1; then
  osacompile -o "$APP" "$ROOT/gui/MacScrub.applescript"
else
  echo "osacompile не найден — пропускаю (только на macOS)." >&2
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
fi

RES="$APP/Contents/Resources"
mkdir -p "$RES/bin"

# 2) Встраиваем движок
cp "$ROOT/bin/macscrub" "$RES/bin/macscrub"
chmod +x "$RES/bin/macscrub"

# 3) Иконка (PNG -> .icns) при наличии инструментов
if [[ -f "$ROOT/assets/icon.png" ]] && command -v sips >/dev/null 2>&1 && command -v iconutil >/dev/null 2>&1; then
  echo "==> Генерирую иконку"
  ICONSET="$(mktemp -d)/MacScrub.iconset"
  mkdir -p "$ICONSET"
  for sz in 16 32 64 128 256 512; do
    sips -z $sz $sz "$ROOT/assets/icon.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
    d=$((sz*2))
    sips -z $d $d "$ROOT/assets/icon.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$RES/applet.icns" 2>/dev/null || true
fi

# 4) Правим Info.plist: имя, версия, идентификатор
PLIST="$APP/Contents/Info.plist"
if [[ -f "$PLIST" ]] && command -v /usr/libexec/PlistBuddy >/dev/null 2>&1; then
  PB=/usr/libexec/PlistBuddy
  $PB -c "Set :CFBundleName MacScrub" "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleName string MacScrub" "$PLIST"
  $PB -c "Set :CFBundleDisplayName MacScrub" "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleDisplayName string MacScrub" "$PLIST"
  $PB -c "Set :CFBundleIdentifier com.macscrub.app" "$PLIST" 2>/dev/null || $PB -c "Add :CFBundleIdentifier string com.macscrub.app" "$PLIST"
  $PB -c "Set :CFBundleShortVersionString 1.0.0" "$PLIST" 2>/dev/null || true
  $PB -c "Set :LSMinimumSystemVersion 10.13" "$PLIST" 2>/dev/null || true
fi

echo "==> Готово: $APP"

# 5) Ярлык на рабочем столе (реальный Finder-алиас, иначе симлинк)
DESKTOP="$HOME/Desktop"
if [[ -d "$DESKTOP" ]]; then
  if command -v osascript >/dev/null 2>&1; then
    osascript >/dev/null 2>&1 <<OSA || ln -sf "$APP" "$DESKTOP/MacScrub"
tell application "Finder"
  set appFile to POSIX file "$APP" as alias
  try
    delete (every item of (desktop) whose name is "MacScrub alias")
  end try
  make new alias file at (path to desktop folder) to appFile
end tell
OSA
    echo "==> Ярлык добавлен на рабочий стол"
  else
    ln -sf "$APP" "$DESKTOP/MacScrub"
    echo "==> Симлинк добавлен на рабочий стол"
  fi
fi

cat <<EOF

Готово! Запустите MacScrub с рабочего стола или из $APP

Если macOS не даёт открыть (Gatekeeper — приложение из интернета),
снимите карантин один раз:
  xattr -dr com.apple.quarantine "$APP"
или откройте правой кнопкой → «Открыть».

Командная строка (без GUI):
  "$RES/bin/macscrub" scan
  "$RES/bin/macscrub" clean --window this-week --apply
EOF
