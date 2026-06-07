#!/usr/bin/env bash
# Spandan local stack launcher.
# Brings up MailPit (local SMTP catch-all) + Streamlit in two background processes,
# tee'd to log files, and traps Ctrl-C to clean both up.
#
# Run from the spandan/ directory:
#     ./run-stack.sh
#
# Then open:
#     http://localhost:8501  (Spandan dashboard)
#     http://localhost:8025  (MailPit inbox)

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d venv ]; then
  echo "venv missing. Create it first: python -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
# shellcheck disable=SC1091
source venv/bin/activate

# Pin to the venv's binaries explicitly. If the user's PATH points at a
# system streamlit (no streamlit-authenticator there), the script still
# works.
PY="$(pwd)/venv/bin/python"
PIP="$PY -m pip"
STREAMLIT="$PY -m streamlit"

# Auto-install missing critical deps (idempotent, fast when already there).
$PY -c "import streamlit_authenticator" >/dev/null 2>&1 || {
  echo "==> Installing streamlit-authenticator + bcrypt into venv ..."
  $PIP install --quiet streamlit-authenticator==0.3.3 bcrypt==4.2.0
}

mkdir -p logs data

echo "==> Stopping any old MailPit/Streamlit on these ports ..."
# Graceful first (TERM gives MailPit time to checkpoint its SQLite WAL —
# critical so previously-stored emails survive across restarts), then
# force-kill anything still hanging around.
for port in 1025 8025 8501; do
    pids="$(lsof -ti :$port 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        kill -TERM $pids 2>/dev/null || true
    fi
done
sleep 2
for port in 1025 8025 8501; do
    pids="$(lsof -ti :$port 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        kill -9 $pids 2>/dev/null || true
    fi
done
sleep 1

echo "==> Starting MailPit (SMTP :1025, web UI :8025) ..."
# Persist messages across restarts to ./data/mailpit.db (NOT /tmp, which
# macOS auto-cleans on reboot or after a few days idle, wiping the
# inbox). Wipe only when explicitly asked:
#   ./run-stack.sh --fresh    -> start with an empty inbox.
MAILPIT_DB="$(pwd)/data/mailpit.db"
# Migrate legacy /tmp DB on first run so users don't lose their old inbox.
if [ -f /tmp/mailpit.db ] && [ ! -f "$MAILPIT_DB" ]; then
    echo "    migrating /tmp/mailpit.db -> $MAILPIT_DB"
    cp /tmp/mailpit.db "$MAILPIT_DB" 2>/dev/null || true
fi
if [ "${1:-}" = "--fresh" ]; then
    echo "    --fresh flag detected — wiping mailpit.db"
    rm -f "$MAILPIT_DB" "$MAILPIT_DB-shm" "$MAILPIT_DB-wal"
fi
# CRITICAL: force-checkpoint any leftover WAL into the main DB before
# we hand the file to mailpit. If a prior run was killed forcefully
# (Activity Monitor, OS reboot, kill -9), the inbox lives in the WAL
# file. sqlite3 ships with macOS by default. If unavailable, mailpit's
# own WAL recovery will still work in most cases.
if [ -f "$MAILPIT_DB" ] && command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$MAILPIT_DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
fi
./tools/mailpit -d "$MAILPIT_DB" --disable-version-check --quiet \
    --smtp 0.0.0.0:1025 --listen 0.0.0.0:8025 \
    > logs/mailpit.log 2>&1 &
MAILPIT_PID=$!
echo "    pid=$MAILPIT_PID  log=logs/mailpit.log"

sleep 2
if ! lsof -ti :1025 >/dev/null 2>&1; then
    echo "MailPit failed to bind :1025. Check logs/mailpit.log"
    exit 1
fi

echo "==> Starting Streamlit (http://localhost:8501) ..."
$STREAMLIT run app/Home.py --server.headless true \
    > logs/streamlit.log 2>&1 &
STREAMLIT_PID=$!
echo "    pid=$STREAMLIT_PID  log=logs/streamlit.log"

cleanup_done=0
cleanup() {
    if [ "$cleanup_done" = "1" ]; then return; fi
    cleanup_done=1
    echo
    echo "==> Shutting down ..."
    # SIGTERM first (lets MailPit close its SQLite connection cleanly),
    # bigger sleep so it has time to flush, then SIGKILL anything still
    # alive. Finally, force a WAL checkpoint so the inbox is durable
    # even if MailPit didn't truncate the WAL on its own.
    kill -TERM "${MAILPIT_PID:-}" 2>/dev/null || true
    kill -TERM "${STREAMLIT_PID:-}" 2>/dev/null || true
    sleep 4
    kill -9 "${MAILPIT_PID:-}" 2>/dev/null || true
    kill -9 "${STREAMLIT_PID:-}" 2>/dev/null || true
    if [ -f "${MAILPIT_DB:-}" ] && command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$MAILPIT_DB" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null 2>&1 || true
    fi
}
# HUP catches "close terminal window" on macOS — without this the bash
# process is killed before the trap can run, orphaning MailPit and
# losing its WAL data.
trap cleanup INT TERM HUP EXIT

cat <<EOF

==========================================================
  Spandan stack is running.
  Dashboard:    http://localhost:8501
  MailPit UI:   http://localhost:8025

  Log files:
    logs/mailpit.log
    logs/streamlit.log

  Press Ctrl-C to stop both.
==========================================================

EOF

# Poll until either process exits — portable across bash 3.2 (macOS
# default) and modern bash. `wait -n` would have been simpler but that
# was added in bash 4.3 and silently exits the script under bash 3.2,
# orphaning MailPit/Streamlit and skipping the cleanup trap.
while kill -0 "$MAILPIT_PID" 2>/dev/null && kill -0 "$STREAMLIT_PID" 2>/dev/null; do
    sleep 2
done
echo
echo "==> One of the processes exited; tearing down the rest ..."
cleanup
