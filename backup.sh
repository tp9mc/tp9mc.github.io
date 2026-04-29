#!/bin/bash
# Daily backup: git push + local archive + /tmp data files
# Runs via launchd at 03:00. Keeps 14 daily archives.

REPO="/Users/timofeev_sd/claude-workspace/tp9mc.github.io"
BDIR="/Users/timofeev_sd/claude-workspace/backups/tp9mc"
DATE=$(date +%Y-%m-%d)
LOG="$BDIR/backup.log"

mkdir -p "$BDIR"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

# ── 1. Git: commit anything uncommitted, then push ────────────
cd "$REPO"
if [[ -n "$(git status --porcelain)" ]]; then
    git add -A
    git commit -m "auto: daily backup $DATE" \
        --author="backup-bot <backup@local>" 2>&1 | tail -1 >> "$LOG"
    log "committed local changes"
fi
git push origin main >> "$LOG" 2>&1 && log "pushed to GitHub" || log "WARN: git push failed"

# ── 2. Archive code (skip .git and heavy assets already in git) ─
ARCHIVE="$BDIR/code-$DATE.tar.gz"
tar -czf "$ARCHIVE" \
    --exclude='.git' \
    --exclude='assets' \
    -C "$(dirname "$REPO")" \
    "$(basename "$REPO")" 2>/dev/null
log "code archive → $(du -sh "$ARCHIVE" | cut -f1)  $ARCHIVE"

# ── 3. Backup /tmp data files (bot stats, catalog) ────────────
for SRC in /tmp/bot_stats.jsonl /tmp/catalog.json; do
    [[ -f "$SRC" ]] || continue
    FNAME="$(basename "$SRC" | sed "s/\\./-$DATE./")"
    cp "$SRC" "$BDIR/$FNAME"
    log "saved $SRC → $BDIR/$FNAME"
done

# ── 4. Rotate: keep last 14 days ──────────────────────────────
find "$BDIR" -maxdepth 1 \( -name "code-*.tar.gz" -o -name "bot_stats-*.jsonl" -o -name "catalog-*.json" \) \
    -mtime +14 -delete
log "rotation done. backup complete."

# ── 5. Restart bot ────────────────────────────────────────────
BOT_PID=$(cat /tmp/propferma_bot.lock 2>/dev/null)
if [[ -n "$BOT_PID" ]] && kill -0 "$BOT_PID" 2>/dev/null; then
    kill "$BOT_PID"
    log "stopped bot (PID $BOT_PID)"
fi
rm -f /tmp/propferma_bot.lock
sleep 2
nohup /usr/local/bin/python3.14 "$REPO/bot.py" >> /tmp/bot.log 2>&1 &
log "bot restarted (PID $!)"
