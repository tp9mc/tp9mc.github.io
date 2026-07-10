#!/bin/zsh
# ============================================================================
#  MacScrub — локальный установщик (всё внутри этого файла).
#  Запуск создаёт готовое приложение MacScrub.app на рабочем столе.
#  Работает офлайн, ничего не качает из интернета, зависимостей нет.
# ============================================================================
emulate -L zsh
set -e

echo ""
echo "  MacScrub — установка локального приложения…"
echo ""

if [[ "$(uname)" != "Darwin" ]]; then
  echo "  Этот установщик работает только на macOS." >&2
  exit 1
fi
if ! command -v osacompile >/dev/null 2>&1; then
  echo "  Не найден osacompile (нужен macOS)." >&2
  exit 1
fi

APP="$HOME/Desktop/MacScrub.app"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1) Движок очистки ---------------------------------------------------------
mkdir -p "$TMP/bin"
cat > "$TMP/bin/macscrub" <<'MACSCRUB_ENGINE_EOF'
#!/bin/zsh
# MacScrub — портативная утилита очистки macOS (аналог CleanMyMac / OnyX).
# Чистый zsh + штатные бинарники macOS (find, rm, du, sqlite3, qlmanage,
# tmutil, launchctl). Никаких внешних зависимостей — работает на любом Mac.
#
# Безопасность:
#   * По умолчанию РЕЖИМ АНАЛИЗА (dry-run). Реальное удаление только с --apply.
#   * Каждый путь проходит проверку белого списка префиксов и чёрного списка
#     критичных каталогов. Пустые/корневые пути отвергаются.
#   * По умолчанию файлы отправляются в Корзину, а не удаляются безвозвратно.
#
# Использование: macscrub <команда> [опции]   (см. `macscrub help`).

emulate -L zsh
setopt no_nomatch pipe_fail
set -u

readonly MS_VERSION="1.0.0"
readonly MS_NAME="MacScrub"
# Путь к самому скрипту (на верхнем уровне $0 = путь; внутри функций — имя функции).
readonly MS_SELF="${0:A}"

# ---------------------------------------------------------------------------
# Пути и состояние
# ---------------------------------------------------------------------------
: ${HOME:?HOME не задан}
MS_SUPPORT_DIR="${MACSCRUB_HOME:-$HOME/Library/Application Support/MacScrub}"
MS_REPORT_DIR="${MACSCRUB_REPORT_DIR:-$MS_SUPPORT_DIR/reports}"
MS_LAUNCH_LABEL="com.macscrub.schedule"
MS_LAUNCH_PLIST="$HOME/Library/LaunchAgents/${MS_LAUNCH_LABEL}.plist"

# Опции по умолчанию
OPT_WINDOW="all"           # this-day | this-week | this-month | all
OPT_APPLY=0                # 0 = dry-run, 1 = реально удалять
OPT_TRASH=1                # 1 = в Корзину, 0 = rm безвозвратно
OPT_JSON=0                 # 1 = машиночитаемый вывод
OPT_YES=0                  # 1 = не спрашивать подтверждение
OPT_INCLUDE_SYSTEM=0       # 1 = трогать системные пути (нужен root)
OPT_CATEGORIES=""          # список id через запятую; пусто = безопасный набор
OPT_EXCLUDES=()            # шаблоны исключений
OPT_ADVISE=0               # 1 = вывести локальные рекомендации после анализа

# Аккумуляторы отчёта
typeset -gA CAT_BYTES CAT_COUNT CAT_RISK CAT_NAME
typeset -ga REPORT_ROWS
REPORT_ERRORS=()
TOTAL_BYTES=0
TOTAL_COUNT=0

# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------
if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\e[0m'; C_DIM=$'\e[2m'; C_B=$'\e[1m'
  C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_BLU=$'\e[34m'; C_CYA=$'\e[36m'
else
  C_RESET=""; C_DIM=""; C_B=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_CYA=""
fi

log()   { print -r -- "$@" >&2; }
info()  { print -r -- "${C_CYA}•${C_RESET} $*" >&2; }
warn()  { print -r -- "${C_YEL}⚠${C_RESET}  $*" >&2; }
err()   { print -r -- "${C_RED}✗${C_RESET} $*" >&2; }
ok()    { print -r -- "${C_GRN}✓${C_RESET} $*" >&2; }
die()   { err "$*"; exit 1; }

human() { # байты -> человекочитаемо
  local b=$1
  if   (( b >= 1073741824 )); then printf '%.2f ГБ' "$(( b / 1073741824.0 ))"
  elif (( b >= 1048576 ));    then printf '%.2f МБ' "$(( b / 1048576.0 ))"
  elif (( b >= 1024 ));       then printf '%.1f КБ' "$(( b / 1024.0 ))"
  else printf '%d Б' "$b"; fi
}

# ---------------------------------------------------------------------------
# Определения категорий
#   Формат: id|Название|risk(low/med/high)|sudo(0/1)|handler|arg
#   handler: files  — arg это список каталогов/файлов (через ';')
#            trash   — очистка Корзины
#            qlthumb — миниатюры QuickLook
#            sqlite  — история браузера, arg = "engine:db1;db2"
#            histfile— файлы истории shell (усечение, не удаление)
# ---------------------------------------------------------------------------
ms_categories() {
  # --- Мусор и кэш ---
  print -r -- "user-caches|Кэш приложений пользователя|low|0|files|$HOME/Library/Caches"
  print -r -- "user-logs|Логи приложений пользователя|low|0|files|$HOME/Library/Logs"
  print -r -- "diagnostic-reports|Отчёты диагностики (крэш-логи)|low|0|files|$HOME/Library/Logs/DiagnosticReports"
  print -r -- "saved-state|Сохранённые состояния приложений|low|0|files|$HOME/Library/Saved Application State"
  print -r -- "trash|Корзина|low|0|trash|"
  print -r -- "temp-folders|Временные файлы (/var/folders)|med|0|tmpfolders|"
  print -r -- "xcode-derived|Xcode DerivedData / кэши|low|0|files|$HOME/Library/Developer/Xcode/DerivedData;$HOME/Library/Developer/CoreSimulator/Caches"
  print -r -- "dev-caches|Кэши сборщиков (npm/yarn/pip/gradle/CocoaPods)|low|0|files|$HOME/.npm/_cacache;$HOME/Library/Caches/Yarn;$HOME/Library/Caches/pip;$HOME/.gradle/caches;$HOME/Library/Caches/CocoaPods"
  print -r -- "homebrew-cache|Кэш Homebrew|low|0|files|$HOME/Library/Caches/Homebrew"
  print -r -- "mail-downloads|Вложения Mail (загрузки)|med|0|files|$HOME/Library/Containers/com.apple.mail/Data/Library/Mail Downloads"
  print -r -- "font-caches|Кэш шрифтов пользователя|low|0|files|$HOME/Library/Caches/com.apple.FontRegistry"

  # --- Следы использования (приватность) ---
  print -r -- "quicklook-thumbnails|Миниатюры QuickLook|low|0|qlthumb|"
  print -r -- "finder-dsstore|.DS_Store и следы Finder|low|0|dsstore|"
  print -r -- "recent-items|Списки «Недавние» (Finder/приложения)|med|0|files|$HOME/Library/Application Support/com.apple.sharedfilelist"
  print -r -- "shell-history|История команд терминала|med|0|histfile|$HOME/.zsh_history;$HOME/.bash_history;$HOME/.sh_history;$HOME/.python_history;$HOME/.node_repl_history;$HOME/.lesshst;$HOME/.psql_history"
  print -r -- "safari-history|История/данные Safari|high|0|sqlite|safari:$HOME/Library/Safari/History.db"
  print -r -- "chrome-history|История Google Chrome|high|0|sqlite|chrome:$HOME/Library/Application Support/Google/Chrome"
  print -r -- "edge-history|История Microsoft Edge|high|0|sqlite|chrome:$HOME/Library/Application Support/Microsoft Edge"
  print -r -- "brave-history|История Brave|high|0|sqlite|chrome:$HOME/Library/Application Support/BraveSoftware/Brave-Browser"
  print -r -- "firefox-history|История Firefox|high|0|sqlite|firefox:$HOME/Library/Application Support/Firefox/Profiles"
  print -r -- "safari-cache|Кэш Safari|low|0|files|$HOME/Library/Caches/com.apple.Safari;$HOME/Library/Containers/com.apple.Safari/Data/Library/Caches"
  print -r -- "chrome-cache|Кэш Chrome|low|0|files|$HOME/Library/Caches/Google/Chrome;$HOME/Library/Application Support/Google/Chrome/Default/Cache"

  # --- Системные (нужен root, только с --include-system) ---
  print -r -- "system-caches|Системный кэш (/Library/Caches)|high|1|files|/Library/Caches"
  print -r -- "system-logs|Системные логи (/private/var/log)|high|1|files|/private/var/log"
  print -r -- "unified-log|Единый журнал системы (log erase)|high|1|unifiedlog|"
  print -r -- "system-diagnostics|Системные крэш-логи|high|1|files|/Library/Logs/DiagnosticReports"
}

# «Безопасный по умолчанию» набор (если категории не заданы явно)
readonly MS_DEFAULT_SET="user-caches,user-logs,diagnostic-reports,saved-state,trash,temp-folders,xcode-derived,dev-caches,homebrew-cache,quicklook-thumbnails,finder-dsstore,font-caches"

# ---------------------------------------------------------------------------
# Проверки безопасности пути
# ---------------------------------------------------------------------------
_protected_exact=(
  "/" "$HOME" "$HOME/" "$HOME/Library" "$HOME/Documents" "$HOME/Desktop"
  "$HOME/Downloads" "$HOME/Pictures" "$HOME/Movies" "$HOME/Music"
  "/System" "/usr" "/bin" "/sbin" "/etc" "/var" "/private" "/Applications"
  "/Library" "/Users" "/opt" "/private/var" "/private/etc"
)

_allowed_prefixes=(
  "$HOME/Library/Caches" "$HOME/Library/Logs" "$HOME/Library/Saved Application State"
  "$HOME/Library/Application Support" "$HOME/Library/Developer" "$HOME/Library/Containers"
  "$HOME/Library/Safari" "$HOME/.Trash" "$HOME/.npm" "$HOME/.gradle" "$HOME/.cache"
  "/private/var/folders" "/Library/Caches" "/private/var/log" "/Library/Logs"
)

_is_protected() {
  local p="$1" x
  for x in $_protected_exact; do [[ "$p" == "$x" ]] && return 0; done
  return 1
}

# Разрешён ли путь к удалению? 0 = да, 1 = нет.
_path_allowed() {
  local p="$1" pre
  [[ -z "$p" ]] && return 1
  [[ "$p" != /* ]] && return 1              # только абсолютные пути
  [[ "$p" == *".."* ]] && return 1           # без обхода вверх
  (( ${#p} < 6 )) && return 1                # слишком короткий => подозрительно
  _is_protected "$p" && return 1
  for pre in $_allowed_prefixes; do
    [[ "$p" == "$pre" || "$p" == "$pre"/* ]] && return 0
  done
  return 1
}

_excluded() {
  local p="$1" pat
  for pat in $OPT_EXCLUDES; do
    [[ "$p" == *"$pat"* ]] && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# Временное окно -> порог для find -newermt
# ---------------------------------------------------------------------------
_window_threshold() { # печатает "YYYY-MM-DD HH:MM:SS" или пусто для all
  case "$OPT_WINDOW" in
    all) print -r -- "" ;;
    this-day)   date -v0H -v0M -v0S "+%Y-%m-%d %H:%M:%S" ;;
    this-week)
      local dow=$(date +%u)          # 1=Пн .. 7=Вс
      date -v-$((dow-1))d -v0H -v0M -v0S "+%Y-%m-%d %H:%M:%S" ;;
    this-month) date -v1d -v0H -v0M -v0S "+%Y-%m-%d %H:%M:%S" ;;
    *) print -r -- "" ;;
  esac
}

_window_epoch() { # unix-секунды начала окна, или пусто
  local ts
  ts=$(_window_threshold) || return 1
  [[ -z "$ts" ]] && { print -r -- ""; return 0; }
  date -j -f "%Y-%m-%d %H:%M:%S" "$ts" "+%s"
}

# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------
_size_of() { # байты одного пути (файл или каталог)
  local p="$1"
  [[ -e "$p" || -L "$p" ]] || { print -r -- 0; return; }
  local kb
  kb=$(du -sk "$p" 2>/dev/null | awk '{print $1}')
  [[ -z "$kb" ]] && kb=0
  print -r -- $(( kb * 1024 ))
}

_trash_dir() { print -r -- "$HOME/.Trash"; }

# Отправить один путь в Корзину (по возможности) или удалить.
_remove_one() {
  local p="$1"
  if (( OPT_TRASH )); then
    local base dest ts
    base="${p:t}"
    ts=$(date "+%Y%m%d-%H%M%S")
    dest="$(_trash_dir)/${base}.macscrub-${ts}"
    /bin/mv -f "$p" "$dest" 2>/dev/null && return 0
    # если mv не удался (напр. другой том) — удаляем
  fi
  /bin/rm -rf "$p" 2>/dev/null
}

# Обработать список путей: посчитать размер, (при --apply) удалить.
# $1 = категория, остальные = пути.
_process_paths() {
  local cat="$1"; shift
  local threshold p sz item_reported
  threshold=$(_window_threshold)

  for p in "$@"; do
    [[ -z "$p" ]] && continue
    if ! _path_allowed "$p"; then
      warn "пропуск (защищённый путь): $p"
      continue
    fi
    [[ -e "$p" || -L "$p" ]] || continue
    _excluded "$p" && continue

    if [[ -z "$threshold" ]]; then
      # окно = all: удаляем содержимое каталога целиком (сам каталог оставляем)
      _collect_and_remove "$cat" "$p" ""
    else
      _collect_and_remove "$cat" "$p" "$threshold"
    fi
  done
}

# Собрать элементы внутри пути с учётом окна и удалить.
_collect_and_remove() {
  local cat="$1" root="$2" threshold="$3"
  local -a items
  if [[ -d "$root" && ! -L "$root" ]]; then
    # содержимое каталога первого уровня
    if [[ -n "$threshold" ]]; then
      items=("${(@f)$(/usr/bin/find "$root" -mindepth 1 -maxdepth 1 -newermt "$threshold" 2>/dev/null)}")
    else
      items=("${(@f)$(/usr/bin/find "$root" -mindepth 1 -maxdepth 1 2>/dev/null)}")
    fi
  else
    # одиночный файл/симлинк
    if [[ -n "$threshold" ]]; then
      /usr/bin/find "$root" -maxdepth 0 -newermt "$threshold" 2>/dev/null | read -r _ && items=("$root") || items=()
    else
      items=("$root")
    fi
  fi

  local it sz
  for it in $items; do
    [[ -z "$it" ]] && continue
    _excluded "$it" && continue
    if ! _path_allowed "$it"; then continue; fi
    sz=$(_size_of "$it")
    _record "$cat" "$it" "$sz"
    if (( OPT_APPLY )); then
      if ! _remove_one "$it"; then
        REPORT_ERRORS+=("не удалось удалить: $it")
      fi
    fi
  done
}

_record() {
  local cat="$1" path="$2" sz="$3"
  CAT_BYTES[$cat]=$(( ${CAT_BYTES[$cat]:-0} + sz ))
  CAT_COUNT[$cat]=$(( ${CAT_COUNT[$cat]:-0} + 1 ))
  TOTAL_BYTES=$(( TOTAL_BYTES + sz ))
  TOTAL_COUNT=$(( TOTAL_COUNT + 1 ))
  (( OPT_JSON )) || print -r -- "    ${C_DIM}${sz}Б${C_RESET}  $path" >&2
}

# ---------------------------------------------------------------------------
# Специальные обработчики
# ---------------------------------------------------------------------------
_handle_trash() {
  local cat="$1" t="$(_trash_dir)"
  [[ -d "$t" ]] || return 0
  _process_paths "$cat" "$t"
}

_handle_tmpfolders() {
  local cat="$1"
  # Пользовательские временные каталоги C (кэш) и T (temp)
  local base
  base=$(getconf DARWIN_USER_CACHE_DIR 2>/dev/null)
  [[ -n "$base" && -d "$base" ]] && _process_paths "$cat" "$base"
  base=$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null)
  [[ -n "$base" && -d "$base" ]] && _process_paths "$cat" "$base"
}

_handle_qlthumb() {
  local cat="$1"
  local dir
  dir=$(getconf DARWIN_USER_CACHE_DIR 2>/dev/null)/com.apple.QuickLook.thumbnailcache
  if [[ -d "$dir" ]]; then
    _process_paths "$cat" "$dir"
  fi
  if (( OPT_APPLY )); then
    command -v qlmanage >/dev/null 2>&1 && qlmanage -r cache >/dev/null 2>&1
  fi
}

_handle_dsstore() {
  local cat="$1" threshold sz f
  threshold=$(_window_threshold)
  # Ищем .DS_Store в домашнем каталоге пользователя (не системные)
  local -a args
  args=("$HOME" -type f -name ".DS_Store")
  [[ -n "$threshold" ]] && args+=(-newermt "$threshold")
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    # .DS_Store безопасны к удалению везде в $HOME
    [[ "$f" == "$HOME"/* ]] || continue
    _excluded "$f" && continue
    sz=$(_size_of "$f")
    _record "$cat" "$f" "$sz"
    (( OPT_APPLY )) && { _remove_one "$f" || REPORT_ERRORS+=("не удалить: $f"); }
  done < <(/usr/bin/find "${args[@]}" 2>/dev/null)
}

_handle_histfile() {
  local cat="$1" spec="$2" f
  local -a files
  files=("${(@s.;.)spec}")
  for f in $files; do
    [[ -f "$f" ]] || continue
    _excluded "$f" && continue
    local sz; sz=$(_size_of "$f")
    _record "$cat" "$f" "$sz"
    if (( OPT_APPLY )); then
      # усекаем, а не удаляем — чтобы shell продолжал работать
      : > "$f" 2>/dev/null || REPORT_ERRORS+=("не удалить историю: $f")
    fi
  done
}

_handle_unifiedlog() {
  local cat="$1"
  (( OPT_INCLUDE_SYSTEM )) || { warn "$cat: нужен --include-system (root)"; return 0; }
  _record "$cat" "unified system log" 0
  if (( OPT_APPLY )); then
    if (( EUID == 0 )); then
      log erase --all 2>/dev/null || REPORT_ERRORS+=("log erase не выполнен")
    else
      REPORT_ERRORS+=("$cat: требуется запуск через sudo")
    fi
  fi
}

# История браузеров с учётом временного окна через sqlite3.
_handle_sqlite() {
  local cat="$1" spec="$2"
  local engine="${spec%%:*}" path="${spec#*:}"
  command -v sqlite3 >/dev/null 2>&1 || { warn "$cat: нет sqlite3"; return 0; }

  local epoch; epoch=$(_window_epoch)
  # Порог в формате конкретного движка
  case "$engine" in
    chrome)  # микросекунды с 1601-01-01
      _sqlite_chrome "$cat" "$path" "$epoch" ;;
    firefox) # микросекунды с 1970-01-01
      _sqlite_firefox "$cat" "$path" "$epoch" ;;
    safari)  # секунды с 2001-01-01 (CFAbsoluteTime)
      _sqlite_safari "$cat" "$path" "$epoch" ;;
  esac
}

# Безопасно ли трогать БД (браузер закрыт)?
_browser_running() { pgrep -qi "$1"; }

_run_sql() { # $1=db $2=sql ; уважает dry-run
  local db="$1" sql="$2"
  [[ -f "$db" ]] || return 1
  if (( OPT_APPLY )); then
    sqlite3 "$db" "$sql" 2>/dev/null || return 2
  fi
  return 0
}

_sqlite_chrome() {
  local cat="$1" base="$2" epoch="$3" prof db
  [[ -d "$base" ]] || return 0
  if _browser_running "Chrome" || _browser_running "Edge" || _browser_running "Brave"; then
    warn "$cat: браузер запущен — пропуск (закройте его)"; return 0
  fi
  # Профили: Default, Profile 1, ...
  for prof in "$base"/Default "$base"/Profile*; do
    db="$prof/History"
    [[ -f "$db" ]] || continue
    local sz; sz=$(_size_of "$db"); _record "$cat" "$db" "$sz"
    if [[ -n "$epoch" ]]; then
      local thr=$(( (epoch + 11644473600) * 1000000 ))
      _run_sql "$db" "DELETE FROM urls WHERE last_visit_time>=$thr; DELETE FROM visits WHERE visit_time>=$thr; VACUUM;"
    else
      _run_sql "$db" "DELETE FROM urls; DELETE FROM visits; VACUUM;"
    fi
  done
}

_sqlite_firefox() {
  local cat="$1" base="$2" epoch="$3" prof db
  [[ -d "$base" ]] || return 0
  if _browser_running "firefox"; then warn "$cat: Firefox запущен — пропуск"; return 0; fi
  for prof in "$base"/*.default* "$base"/*; do
    db="$prof/places.sqlite"
    [[ -f "$db" ]] || continue
    local sz; sz=$(_size_of "$db"); _record "$cat" "$db" "$sz"
    if [[ -n "$epoch" ]]; then
      local thr=$(( epoch * 1000000 ))
      _run_sql "$db" "DELETE FROM moz_historyvisits WHERE visit_date>=$thr; DELETE FROM moz_places WHERE last_visit_date>=$thr AND id NOT IN (SELECT place_id FROM moz_bookmarks WHERE fk IS NOT NULL); VACUUM;"
    else
      _run_sql "$db" "DELETE FROM moz_historyvisits; VACUUM;"
    fi
  done
}

_sqlite_safari() {
  local cat="$1" db="$2" epoch="$3"
  [[ -f "$db" ]] || return 0
  if _browser_running "Safari"; then warn "$cat: Safari запущен — пропуск"; return 0; fi
  local sz; sz=$(_size_of "$db"); _record "$cat" "$db" "$sz"
  if [[ -n "$epoch" ]]; then
    local thr=$(( epoch - 978307200 ))
    _run_sql "$db" "DELETE FROM history_visits WHERE visit_time>=$thr; DELETE FROM history_items WHERE id NOT IN (SELECT history_item FROM history_visits); VACUUM;"
  else
    _run_sql "$db" "DELETE FROM history_visits; DELETE FROM history_items; VACUUM;"
  fi
}

# ---------------------------------------------------------------------------
# Диспетчер категории
# ---------------------------------------------------------------------------
_run_category() {
  local line="$1"
  local id name risk sudo handler arg
  IFS='|' read -r id name risk sudo handler arg <<< "$line"

  if [[ "$sudo" == "1" ]] && (( ! OPT_INCLUDE_SYSTEM )); then
    return 0   # системные категории пропускаем без --include-system
  fi

  CAT_RISK[$id]="$risk"
  CAT_NAME[$id]="$name"
  (( OPT_JSON )) || info "${C_B}${name}${C_RESET} ${C_DIM}[$id, риск:$risk]${C_RESET}"

  case "$handler" in
    files)     _process_paths "$id" "${(@s.;.)arg}" ;;
    trash)     _handle_trash "$id" ;;
    tmpfolders)_handle_tmpfolders "$id" ;;
    qlthumb)   _handle_qlthumb "$id" ;;
    dsstore)   _handle_dsstore "$id" ;;
    histfile)  _handle_histfile "$id" "$arg" ;;
    unifiedlog)_handle_unifiedlog "$id" ;;
    sqlite)    _handle_sqlite "$id" "$arg" ;;
    *)         warn "неизвестный обработчик: $handler" ;;
  esac
}

# Список категорий к выполнению (с учётом --categories)
_selected_categories() {
  local want
  if [[ -n "$OPT_CATEGORIES" ]]; then
    want="$OPT_CATEGORIES"
  else
    want="$MS_DEFAULT_SET"
  fi
  local -A want_set
  local c
  for c in "${(@s.,.)want}"; do want_set[$c]=1; done

  ms_categories | while IFS= read -r line; do
    local id="${line%%|*}"
    if [[ "$want" == "all-safe" ]]; then
      # все несистемные
      local sudo; sudo=$(print -r -- "$line" | cut -d'|' -f4)
      [[ "$sudo" == "0" ]] && print -r -- "$line"
    elif [[ "$want" == "all" ]]; then
      print -r -- "$line"
    elif [[ -n "${want_set[$id]:-}" ]]; then
      print -r -- "$line"
    fi
  done
}

# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------
_write_report() {
  mkdir -p "$MS_REPORT_DIR" 2>/dev/null || return 1
  local ts=$(date "+%Y-%m-%d_%H-%M-%S")
  local mode; (( OPT_APPLY )) && mode="apply" || mode="dry-run"
  local base="$MS_REPORT_DIR/report_${ts}_${mode}"
  local txt="${base}.txt" json="${base}.json"

  {
    print -r -- "MacScrub v$MS_VERSION — отчёт об очистке"
    print -r -- "Дата:            $(date '+%Y-%m-%d %H:%M:%S %Z')"
    print -r -- "Режим:           $( (( OPT_APPLY )) && echo 'РЕАЛЬНОЕ УДАЛЕНИЕ' || echo 'анализ (dry-run)')"
    print -r -- "Способ:          $( (( OPT_TRASH )) && echo 'в Корзину' || echo 'безвозвратно')"
    print -r -- "Период:          $OPT_WINDOW"
    print -r -- "Системные:       $( (( OPT_INCLUDE_SYSTEM )) && echo 'да' || echo 'нет')"
    print -r -- "----------------------------------------------------------------"
    local id
    for id in ${(k)CAT_BYTES}; do
      printf '  %-24s %8s элем.  %s\n' "$id" "${CAT_COUNT[$id]}" "$(human ${CAT_BYTES[$id]})"
    done
    print -r -- "----------------------------------------------------------------"
    printf '  %-24s %8s элем.  %s\n' "ИТОГО" "$TOTAL_COUNT" "$(human $TOTAL_BYTES)"
    if (( ${#REPORT_ERRORS} )); then
      print -r -- ""
      print -r -- "Ошибки:"
      local e; for e in $REPORT_ERRORS; do print -r -- "  - $e"; done
    fi
  } > "$txt"

  # JSON
  {
    print -r -- "{"
    printf '  "tool": "MacScrub", "version": "%s",\n' "$MS_VERSION"
    printf '  "date": "%s",\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
    printf '  "mode": "%s", "method": "%s", "window": "%s", "include_system": %s,\n' \
      "$mode" "$( (( OPT_TRASH )) && echo trash || echo delete)" "$OPT_WINDOW" \
      "$( (( OPT_INCLUDE_SYSTEM )) && echo true || echo false)"
    printf '  "total_bytes": %s, "total_items": %s,\n' "$TOTAL_BYTES" "$TOTAL_COUNT"
    print -r -- '  "categories": {'
    local first=1
    for id in ${(k)CAT_BYTES}; do
      (( first )) || print -r -- ","
      first=0
      printf '    "%s": {"items": %s, "bytes": %s}' "$id" "${CAT_COUNT[$id]}" "${CAT_BYTES[$id]}"
    done
    print -r -- ""
    print -r -- "  },"
    printf '  "errors": %s\n' "$( (( ${#REPORT_ERRORS} )) && echo "${#REPORT_ERRORS}" || echo 0)"
    print -r -- "}"
  } > "$json"

  print -r -- "$txt"
}

# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------
cmd_clean() {
  local threshold; threshold=$(_window_threshold)

  if (( OPT_APPLY )) && (( ! OPT_YES )); then
    print -r -- "" >&2
    warn "Будет выполнено РЕАЛЬНОЕ удаление (период: $OPT_WINDOW, способ: $( (( OPT_TRASH )) && echo Корзина || echo безвозвратно))."
    print -n -- "Продолжить? [y/N] " >&2
    local ans; read -r ans
    [[ "$ans" == [yY]* ]] || { info "Отменено."; return 1; }
  fi

  (( OPT_JSON )) || {
    info "Режим: $( (( OPT_APPLY )) && echo "${C_RED}РЕАЛЬНОЕ УДАЛЕНИЕ${C_RESET}" || echo "${C_GRN}анализ (dry-run)${C_RESET}")"
    info "Период: $OPT_WINDOW"
  }

  local line
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    _run_category "$line"
  done < <(_selected_categories)

  local report
  report=$(_write_report)

  if (( OPT_JSON )); then
    # финальный JSON в stdout = содержимое json-отчёта
    cat "${report%.txt}.json"
  else
    print -r -- "" >&2
    ok "Итог: $(human $TOTAL_BYTES) в $TOTAL_COUNT элементах"
    (( OPT_APPLY )) || warn "Это был АНАЛИЗ. Для реальной очистки добавьте --apply"
    info "Отчёт: $report"
  fi

  (( OPT_ADVISE )) && _print_advice
  return 0
}

cmd_scan() { OPT_APPLY=0; cmd_clean; }

# Локальный советник — работает офлайн, без сети и API. Анализирует итоги
# сканирования (размеры и уровни риска категорий) и печатает рекомендации.
_print_advice() {
  print -r -- "" >&2
  print -r -- "${C_B}────────────────────────────────────────────────${C_RESET}" >&2
  print -r -- "${C_B} Рекомендации (локальный анализ)${C_RESET}" >&2
  print -r -- "${C_B}────────────────────────────────────────────────${C_RESET}" >&2

  if (( TOTAL_COUNT == 0 )); then
    print -r -- "  Мусора по выбранным категориям не найдено — чистить нечего." >&2
    return 0
  fi

  print -r -- "  Всего можно освободить: ${C_B}$(human $TOTAL_BYTES)${C_RESET} (${TOTAL_COUNT} элементов)." >&2

  # Разбиваем категории по уровню риска
  local id
  local -a safe_ids careful_ids
  local safe_bytes=0 careful_bytes=0
  for id in ${(k)CAT_BYTES}; do
    (( ${CAT_BYTES[$id]:-0} == 0 && ${CAT_COUNT[$id]:-0} == 0 )) && continue
    if [[ "${CAT_RISK[$id]}" == "low" ]]; then
      safe_ids+=("$id"); safe_bytes=$(( safe_bytes + ${CAT_BYTES[$id]:-0} ))
    else
      careful_ids+=("$id"); careful_bytes=$(( careful_bytes + ${CAT_BYTES[$id]:-0} ))
    fi
  done

  if (( ${#safe_ids} )); then
    print -r -- "" >&2
    print -r -- "  ${C_GRN}Безопасно чистить сразу${C_RESET} (кэш/логи/временное, ~$(human $safe_bytes)):" >&2
    for id in ${safe_ids}; do
      print -r -- "    ${C_GRN}•${C_RESET} ${CAT_NAME[$id]:-$id} — $(human ${CAT_BYTES[$id]:-0})" >&2
    done
  fi

  if (( ${#careful_ids} )); then
    print -r -- "" >&2
    print -r -- "  ${C_YEL}С осторожностью${C_RESET} (следы использования, ~$(human $careful_bytes)):" >&2
    for id in ${careful_ids}; do
      print -r -- "    ${C_YEL}•${C_RESET} ${CAT_NAME[$id]:-$id} — $(human ${CAT_BYTES[$id]:-0})" >&2
    done
    print -r -- "" >&2
    print -r -- "  ${C_DIM}Перед чисткой истории браузеров закройте их. Удаление истории,${C_RESET}" >&2
    print -r -- "  ${C_DIM}логов и следов необратимо — сначала запустите анализ (scan).${C_RESET}" >&2
  fi

  # Совет по способу удаления
  print -r -- "" >&2
  if (( OPT_TRASH )); then
    print -r -- "  ${C_DIM}Совет: файлы уйдут в Корзину — можно восстановить. Для безвозвратного${C_RESET}" >&2
    print -r -- "  ${C_DIM}удаления добавьте --delete.${C_RESET}" >&2
  else
    print -r -- "  ${C_RED}Внимание: выбрано безвозвратное удаление (--delete).${C_RESET}" >&2
  fi
  return 0
}

cmd_advise() { OPT_APPLY=0; OPT_ADVISE=1; OPT_JSON=0; cmd_clean; }

cmd_categories() {
  if (( OPT_JSON )); then
    print -r -- "["
    local first=1 line
    while IFS='|' read -r id name risk sudo handler arg; do
      (( first )) || print -r -- ","
      first=0
      printf '  {"id":"%s","name":"%s","risk":"%s","sudo":%s}' \
        "$id" "$name" "$risk" "$( [[ $sudo == 1 ]] && echo true || echo false)"
    done < <(ms_categories)
    print -r -- ""
    print -r -- "]"
  else
    printf '%-22s %-6s %-5s %s\n' "ID" "риск" "sudo" "Описание"
    printf '%-22s %-6s %-5s %s\n' "──" "────" "────" "────────"
    while IFS='|' read -r id name risk sudo handler arg; do
      printf '%-22s %-6s %-5s %s\n' "$id" "$risk" "$( [[ $sudo == 1 ]] && echo да || echo нет)" "$name"
    done < <(ms_categories)
  fi
}

cmd_report() {
  local sub="${1:-list}"
  mkdir -p "$MS_REPORT_DIR" 2>/dev/null
  case "$sub" in
    list)
      local f found=0
      for f in "$MS_REPORT_DIR"/report_*.txt(N.om); do
        found=1
        printf '  %s  (%s)\n' "${f:t}" "$(human $(_size_of "$f"))"
      done
      (( found )) || info "Отчётов нет ($MS_REPORT_DIR)"
      ;;
    show)
      local latest
      latest=(${MS_REPORT_DIR}/report_*.txt(N.om))
      [[ ${#latest} -gt 0 ]] && cat "${latest[1]}" || info "Отчётов нет"
      ;;
    clear|delete)
      local n=0 f
      for f in "$MS_REPORT_DIR"/report_*(N); do rm -f "$f" && (( n++ )); done
      ok "Удалено отчётов: $n"
      ;;
    dir) print -r -- "$MS_REPORT_DIR" ;;
    *) die "report: неизвестная подкоманда '$sub' (list|show|clear|dir)" ;;
  esac
}

cmd_schedule() {
  local sub="${1:-status}"; shift 2>/dev/null || true
  case "$sub" in
    install)
      # Параметры расписания берём из уже разобранных опций + частота
      local freq="${MS_FREQ:-daily}" hour="${MS_HOUR:-3}" minute="${MS_MIN:-0}" weekday="${MS_WEEKDAY:-1}"
      local self="${MACSCRUB_BIN:-$MS_SELF}"
      self="${self:A}"
      mkdir -p "${MS_LAUNCH_PLIST:h}"

      # Собираем аргументы запуска
      local -a run_args
      run_args=("$self" "clean" "--apply" "--yes" "--window" "$OPT_WINDOW")
      (( OPT_TRASH )) || run_args+=("--delete")
      [[ -n "$OPT_CATEGORIES" ]] && run_args+=("--categories" "$OPT_CATEGORIES")

      local cal
      if [[ "$freq" == "weekly" ]]; then
        cal="    <key>Weekday</key><integer>${weekday}</integer>
    <key>Hour</key><integer>${hour}</integer>
    <key>Minute</key><integer>${minute}</integer>"
      else
        cal="    <key>Hour</key><integer>${hour}</integer>
    <key>Minute</key><integer>${minute}</integer>"
      fi

      {
        print -r -- '<?xml version="1.0" encoding="UTF-8"?>'
        print -r -- '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        print -r -- '<plist version="1.0"><dict>'
        print -r -- "  <key>Label</key><string>${MS_LAUNCH_LABEL}</string>"
        print -r -- "  <key>ProgramArguments</key><array>"
        local a; for a in $run_args; do print -r -- "    <string>${a}</string>"; done
        print -r -- "  </array>"
        print -r -- "  <key>StartCalendarInterval</key><dict>"
        print -r -- "$cal"
        print -r -- "  </dict>"
        print -r -- "  <key>StandardOutPath</key><string>${MS_SUPPORT_DIR}/schedule.log</string>"
        print -r -- "  <key>StandardErrorPath</key><string>${MS_SUPPORT_DIR}/schedule.err.log</string>"
        print -r -- "  <key>RunAtLoad</key><false/>"
        print -r -- "</dict></plist>"
      } > "$MS_LAUNCH_PLIST"

      launchctl unload "$MS_LAUNCH_PLIST" 2>/dev/null
      if launchctl load "$MS_LAUNCH_PLIST" 2>/dev/null; then
        ok "Расписание установлено: $freq в $(printf '%02d:%02d' $hour $minute)"
        info "plist: $MS_LAUNCH_PLIST"
      else
        die "не удалось загрузить launchd-агент"
      fi
      ;;
    uninstall)
      launchctl unload "$MS_LAUNCH_PLIST" 2>/dev/null
      rm -f "$MS_LAUNCH_PLIST" && ok "Расписание удалено" || info "Расписание не было установлено"
      ;;
    status)
      if [[ -f "$MS_LAUNCH_PLIST" ]]; then
        ok "Расписание установлено: $MS_LAUNCH_PLIST"
        launchctl list 2>/dev/null | grep -q "$MS_LAUNCH_LABEL" && info "агент загружен" || warn "агент не загружен"
      else
        info "Расписание не установлено"
      fi
      ;;
    *) die "schedule: неизвестная подкоманда '$sub' (install|uninstall|status)" ;;
  esac
}

cmd_help() {
  cat <<EOF
${C_B}${MS_NAME}${C_RESET} v${MS_VERSION} — портативная очистка macOS

${C_B}КОМАНДЫ${C_RESET}
  scan                 анализ без удаления (сколько можно освободить)
  advise               анализ + локальные рекомендации (офлайн, без сети)
  clean                очистка (по умолчанию тоже анализ; удаление с --apply)
  categories           список категорий очистки
  report list|show|clear|dir
                       управление отчётами
  schedule install|uninstall|status
                       запуск по расписанию (launchd)
  help                 эта справка

${C_B}ОПЦИИ${C_RESET}
  --window <w>         период: this-day | this-week | this-month | all (по умолч. all)
  --categories <ids>   список id через запятую, либо all-safe / all
  --apply              РЕАЛЬНО удалять (иначе dry-run)
  --delete             удалять безвозвратно (иначе — в Корзину)
  --include-system     трогать системные пути (нужен sudo)
  --exclude <текст>    исключить пути, содержащие текст (можно повторять)
  --json               машиночитаемый вывод
  --yes                не спрашивать подтверждение
  --report-dir <путь>  каталог отчётов

${C_B}РАСПИСАНИЕ (доп. опции для schedule install)${C_RESET}
  --freq daily|weekly  частота (по умолч. daily)
  --at HH:MM           время запуска (по умолч. 03:00)
  --weekday <1-7>      день недели для weekly (1=Пн)

${C_B}ПРИМЕРЫ${C_RESET}
  macscrub scan
  macscrub clean --window this-week --apply
  macscrub clean --categories user-caches,trash --apply --delete
  macscrub clean --window this-month --include-system --apply     # через sudo
  macscrub schedule install --freq daily --at 03:00 --window this-day
  macscrub report show

${C_DIM}Безопасность: пути вне белого списка не удаляются; по умолчанию — в Корзину.${C_RESET}
EOF
}

# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------
MS_FREQ="daily"; MS_HOUR=3; MS_MIN=0; MS_WEEKDAY=1

parse_common() {
  while (( $# )); do
    case "$1" in
      --window)        OPT_WINDOW="$2"; shift 2 ;;
      --categories)    OPT_CATEGORIES="$2"; shift 2 ;;
      --apply)         OPT_APPLY=1; shift ;;
      --dry-run)       OPT_APPLY=0; shift ;;
      --delete)        OPT_TRASH=0; shift ;;
      --trash)         OPT_TRASH=1; shift ;;
      --include-system)OPT_INCLUDE_SYSTEM=1; shift ;;
      --json)          OPT_JSON=1; shift ;;
      --yes|-y)        OPT_YES=1; shift ;;
      --exclude)       OPT_EXCLUDES+=("$2"); shift 2 ;;
      --report-dir)    MS_REPORT_DIR="$2"; shift 2 ;;
      --freq)          MS_FREQ="$2"; shift 2 ;;
      --at)            MS_HOUR="${2%%:*}"; MS_MIN="${2##*:}"; MS_HOUR=$((10#$MS_HOUR)); MS_MIN=$((10#$MS_MIN)); shift 2 ;;
      --weekday)       MS_WEEKDAY="$2"; shift 2 ;;
      *)               warn "неизвестная опция: $1"; shift ;;
    esac
  done
  case "$OPT_WINDOW" in this-day|this-week|this-month|all) ;; *) die "неверный --window: $OPT_WINDOW";; esac
}

# Загрузка постоянных настроек из config.env (KEY=value). CLI-опции важнее.
_load_config() {
  local cfg="${MACSCRUB_CONFIG:-$MS_SUPPORT_DIR/config.env}"
  [[ -f "$cfg" ]] || return 0
  local line key val
  while IFS='=' read -r key val; do
    key="${key## }"; key="${key%% }"
    [[ -z "$key" || "$key" == \#* ]] && continue
    val="${val%%#*}"; val="${val## }"; val="${val%% }"
    case "$key" in
      WINDOW)         [[ -n "$val" ]] && OPT_WINDOW="$val" ;;
      CATEGORIES)     [[ -n "$val" ]] && OPT_CATEGORIES="$val" ;;
      METHOD)         [[ "$val" == delete ]] && OPT_TRASH=0; [[ "$val" == trash ]] && OPT_TRASH=1 ;;
      INCLUDE_SYSTEM) [[ "$val" == 1 || "$val" == true ]] && OPT_INCLUDE_SYSTEM=1 ;;
    esac
  done < "$cfg"
}

main() {
  _load_config
  local cmd="${1:-help}"; shift 2>/dev/null || true
  case "$cmd" in
    scan)       parse_common "$@"; cmd_scan ;;
    clean)      parse_common "$@"; cmd_clean ;;
    advise)     parse_common "$@"; cmd_advise ;;
    categories) parse_common "$@"; cmd_categories ;;
    report)     local sub="${1:-list}"; shift 2>/dev/null || true; parse_common "$@"; cmd_report "$sub" ;;
    schedule)   local sub="${1:-status}"; shift 2>/dev/null || true; parse_common "$@"; cmd_schedule "$sub" ;;
    version|--version|-v) print -r -- "$MS_NAME $MS_VERSION" ;;
    help|--help|-h) cmd_help ;;
    *) err "неизвестная команда: $cmd"; cmd_help; exit 1 ;;
  esac
}

main "$@"
MACSCRUB_ENGINE_EOF
chmod +x "$TMP/bin/macscrub"

# 2) Интерфейс --------------------------------------------------------------
cat > "$TMP/MacScrub.applescript" <<'MACSCRUB_GUI_EOF'
-- MacScrub GUI — нативный интерфейс на AppleScript (без зависимостей).
-- Компилируется в MacScrub.app и вызывает движок Contents/Resources/bin/macscrub.

property pTitle : "MacScrub — очистка macOS"

on binPath()
	set appPath to POSIX path of (path to me)
	return appPath & "Contents/Resources/bin/macscrub"
end binPath

-- Выполнить движок с аргументами, вернуть stdout+stderr.
on runEngine(args)
	set cmd to quoted form of binPath() & " " & args & " 2>&1"
	try
		return do shell script cmd
	on error errMsg
		return "Ошибка: " & errMsg
	end try
end runEngine

-- Выбор периода очистки.
on chooseWindow()
	set opts to {"Этот день", "Эта неделя", "Этот месяц", "Всё время"}
	set sel to choose from list opts with prompt "За какой период чистить следы?" default items {"Этот день"} without multiple selections allowed
	if sel is false then error number -128
	set s to item 1 of sel
	if s is "Этот день" then
		return "this-day"
	else if s is "Эта неделя" then
		return "this-week"
	else if s is "Этот месяц" then
		return "this-month"
	else
		return "all"
	end if
end chooseWindow

-- Выбор категорий. Возвращает строку id через запятую или "" (значит набор по умолчанию).
on chooseCategories()
	set raw to runEngine("categories")
	set catLines to paragraphs of raw
	set displayList to {}
	set idList to {}
	repeat with ln in catLines
		set lns to (ln as text)
		if lns is not "" and lns does not start with "ID" and lns does not start with "──" then
			-- формат: id  риск  sudo  Описание
			set AppleScript's text item delimiters to " "
			set firstWord to text item 1 of lns
			set AppleScript's text item delimiters to ""
			if firstWord is not "" then
				set end of idList to firstWord
				set end of displayList to lns
			end if
		end if
	end repeat
	set sel to choose from list displayList with prompt "Выберите категории очистки (Cmd для нескольких). Отмена = безопасный набор по умолчанию." with multiple selections allowed
	if sel is false then return ""
	set ids to {}
	repeat with chosen in sel
		set chosenText to (chosen as text)
		set AppleScript's text item delimiters to " "
		set cid to text item 1 of chosenText
		set AppleScript's text item delimiters to ""
		set end of ids to cid
	end repeat
	set AppleScript's text item delimiters to ","
	set res to ids as text
	set AppleScript's text item delimiters to ""
	return res
end chooseCategories

on openLatestReport()
	set repDir to paragraph -1 of runEngine("report dir")
	try
		-- открыть самый свежий .txt-отчёт в TextEdit; иначе показать папку
		do shell script "f=$(ls -t " & quoted form of repDir & "/report_*.txt 2>/dev/null | head -1); " & ¬
			"if [ -n \"$f\" ]; then open -e \"$f\"; else open " & quoted form of repDir & "; fi"
	end try
end openLatestReport

-- Анализ (dry-run)
on doScan()
	set win to chooseWindow()
	set cats to chooseCategories()
	set extra to " --window " & win
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("scan" & extra)
	set summary to my tailLines(out, 6)
	display dialog "АНАЛИЗ завершён (ничего не удалено)." & return & return & summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doScan

-- Очистка (реальное удаление)
on doClean()
	set win to chooseWindow()
	set cats to chooseCategories()
	set methodSel to choose from list {"В Корзину (безопасно)", "Удалить безвозвратно"} with prompt "Способ удаления:" default items {"В Корзину (безопасно)"} without multiple selections allowed
	if methodSel is false then error number -128
	set delFlag to ""
	if (item 1 of methodSel) is "Удалить безвозвратно" then set delFlag to " --delete"

	set confirm to display dialog "Будет выполнено РЕАЛЬНОЕ удаление." & return & "Период: " & win & return & "Отмена возможна только сейчас." buttons {"Отмена", "Очистить"} default button "Отмена" with icon caution with title pTitle
	if button returned of confirm is "Отмена" then return

	set extra to " --apply --yes --window " & win & delFlag
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("clean" & extra)
	set summary to my tailLines(out, 6)
	display dialog "ОЧИСТКА завершена." & return & return & summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doClean

-- Управление отчётами
on doReports()
	set out to runEngine("report list")
	set act to display dialog "Отчёты:" & return & return & out buttons {"Удалить все", "Открыть папку", "Закрыть"} default button "Закрыть" with title pTitle
	if button returned of act is "Удалить все" then
		set c to display dialog "Удалить ВСЕ отчёты?" buttons {"Отмена", "Удалить"} default button "Отмена" with icon caution
		if button returned of c is "Удалить" then
			runEngine("report clear")
			display dialog "Отчёты удалены." buttons {"ОК"} default button "ОК" with title pTitle
		end if
	else if button returned of act is "Открыть папку" then
		set rep to runEngine("report dir")
		do shell script "open " & quoted form of (paragraph -1 of rep)
	end if
end doReports

-- Расписание
on doSchedule()
	set st to runEngine("schedule status")
	set act to choose from list {"Установить ежедневно", "Установить еженедельно", "Удалить расписание", "Статус"} with prompt ("Текущий статус:" & return & st) without multiple selections allowed
	if act is false then return
	set a to item 1 of act
	if a is "Статус" then
		display dialog st buttons {"ОК"} default button "ОК" with title pTitle
		return
	else if a is "Удалить расписание" then
		set out to runEngine("schedule uninstall")
		display dialog out buttons {"ОК"} default button "ОК" with title pTitle
		return
	end if

	set tm to text returned of (display dialog "Время запуска (ЧЧ:ММ):" default answer "03:00" with title pTitle)
	set win to chooseWindow()
	set freq to "daily"
	set extraDay to ""
	if a is "Установить еженедельно" then
		set freq to "weekly"
		set wd to text returned of (display dialog "День недели (1=Пн … 7=Вс):" default answer "1" with title pTitle)
		set extraDay to " --weekday " & wd
	end if
	set out to runEngine("schedule install --freq " & freq & " --at " & tm & " --window " & win & extraDay)
	display dialog out buttons {"ОК"} default button "ОК" with title pTitle
end doSchedule

-- Рекомендации (локальный анализ, без сети)
on doAdvise()
	set win to chooseWindow()
	set cats to chooseCategories()
	set extra to " --window " & win
	if cats is not "" then set extra to extra & " --categories " & quoted form of cats
	set out to runEngine("advise" & extra)
	-- показываем блок рекомендаций (хвост вывода)
	set summary to my tailLines(out, 18)
	display dialog summary buttons {"Открыть отчёт", "ОК"} default button "ОК" with title pTitle
	if button returned of result is "Открыть отчёт" then my openLatestReport()
end doAdvise

on tailLines(txt, n)
	set ps to paragraphs of txt
	set c to count of ps
	if c ≤ n then return txt
	set startI to c - n + 1
	set AppleScript's text item delimiters to return
	set res to (items startI thru c of ps) as text
	set AppleScript's text item delimiters to ""
	return res
end tailLines

on run
	repeat
		set choice to choose from list ¬
			{"Анализ (посмотреть, сколько мусора)", "Очистка (удалить)", "Рекомендации", "Отчёты", "Расписание", "Выход"} ¬
			with prompt "MacScrub — что делаем?" with title pTitle without multiple selections allowed
		if choice is false then exit repeat
		set c to item 1 of choice
		try
			if c starts with "Анализ" then
				doScan()
			else if c starts with "Очистка" then
				doClean()
			else if c is "Рекомендации" then
				doAdvise()
			else if c is "Отчёты" then
				doReports()
			else if c is "Расписание" then
				doSchedule()
			else
				exit repeat
			end if
		on error errMsg number errNum
			if errNum is not -128 then display dialog "Ошибка: " & errMsg buttons {"ОК"} default button "ОК"
		end try
	end repeat
end run
MACSCRUB_GUI_EOF

# 3) Иконка -----------------------------------------------------------------
cat > "$TMP/icon.b64" <<'MACSCRUB_ICON_EOF'
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
AElFTkSuQmCC
MACSCRUB_ICON_EOF
base64 -D -i "$TMP/icon.b64" -o "$TMP/icon.png" 2>/dev/null \
  || base64 --decode "$TMP/icon.b64" > "$TMP/icon.png" 2>/dev/null || true

# 4) Сборка .app ------------------------------------------------------------
rm -rf "$APP"
osacompile -o "$APP" "$TMP/MacScrub.applescript"
mkdir -p "$APP/Contents/Resources/bin"
cp "$TMP/bin/macscrub" "$APP/Contents/Resources/bin/macscrub"
chmod +x "$APP/Contents/Resources/bin/macscrub"

# иконка приложения
if [[ -f "$TMP/icon.png" ]] && command -v sips >/dev/null && command -v iconutil >/dev/null; then
  ICONSET="$TMP/MacScrub.iconset"; mkdir -p "$ICONSET"
  for sz in 16 32 128 256 512; do
    sips -z $sz $sz "$TMP/icon.png" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null 2>&1 || true
    d=$((sz*2)); sips -z $d $d "$TMP/icon.png" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/applet.icns" 2>/dev/null || true
fi

# имя/идентификатор
PLIST="$APP/Contents/Info.plist"
if [[ -f "$PLIST" ]] && [[ -x /usr/libexec/PlistBuddy ]]; then
  /usr/libexec/PlistBuddy -c "Set :CFBundleName MacScrub" "$PLIST" 2>/dev/null || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.macscrub.app" "$PLIST" 2>/dev/null || true
fi

# снять карантин, чтобы открывалось без предупреждений
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

echo "  ✓ Готово! На рабочем столе появилось приложение MacScrub."
echo "    Запусти его двойным кликом."
echo ""
open "$HOME/Desktop" 2>/dev/null || true
