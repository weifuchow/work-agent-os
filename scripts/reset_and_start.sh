#!/usr/bin/env bash
# Reset runtime data and start all long-running services for local development.
# Intended for Git Bash on Windows.
#
# Usage:
#   bash scripts/reset_and_start.sh
#   bash scripts/reset_and_start.sh --no-wait

set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
LOG_DIR="$PROJECT_ROOT/.tmp/run-logs"
NO_WAIT=0

if [[ "${1:-}" == "--no-wait" ]]; then
    NO_WAIT=1
fi

API_PID=""
WORKER_PID=""
SCHEDULER_PID=""
FRONTEND_PID=""

echo "=== Work Agent OS: Reset & Start ==="
echo "Project root: $PROJECT_ROOT"

if ! command -v python >/dev/null 2>&1; then
    echo "python not found in PATH"
    exit 1
fi

if ! command -v npm.cmd >/dev/null 2>&1 && ! command -v npm >/dev/null 2>&1; then
    echo "npm not found in PATH"
    exit 1
fi

if ! command -v powershell.exe >/dev/null 2>&1; then
    echo "powershell.exe not found in PATH"
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "curl not found in PATH"
    exit 1
fi

mkdir -p "$LOG_DIR"

kill_pid_tree() {
    local pid="$1"
    if [[ -n "$pid" ]] && powershell.exe -NoProfile -Command "Get-Process -Id $pid -ErrorAction SilentlyContinue | Out-Null" >/dev/null 2>&1; then
        taskkill //F //T //PID "$pid" >/dev/null 2>&1 || true
    fi
}

cleanup() {
    echo ""
    echo "Stopping started services..."
    kill_pid_tree "$FRONTEND_PID"
    kill_pid_tree "$SCHEDULER_PID"
    kill_pid_tree "$WORKER_PID"
    kill_pid_tree "$API_PID"
}

trap cleanup EXIT INT TERM

kill_matching_processes() {
    local regex="$1"
    powershell.exe -NoProfile -Command "
        \$regex = '$regex';
        Get-CimInstance Win32_Process |
        Where-Object { \$_.CommandLine -and \$_.CommandLine -match \$regex } |
        ForEach-Object {
            Write-Output ('  Killing PID ' + \$_.ProcessId + ' :: ' + \$_.CommandLine);
            Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    " | tr -d '\r' || true
}

wait_for_http() {
    local url="$1"
    local label="$2"
    local attempts="${3:-30}"
    local i

    for ((i = 1; i <= attempts; i++)); do
        if curl -fsS "$url" >/dev/null 2>&1; then
            echo "  $label is ready: $url"
            return 0
        fi
        sleep 1
    done

    echo "  $label failed to become ready: $url"
    return 1
}

ensure_pid_alive() {
    local pid="$1"
    local label="$2"
    if ! kill -0 "$pid" >/dev/null 2>&1; then
        echo "  $label exited unexpectedly (PID $pid)"
        return 1
    fi
    return 0
}

NPM_BIN="npm.cmd"
if ! command -v "$NPM_BIN" >/dev/null 2>&1; then
    NPM_BIN="npm"
fi

echo ""
echo "[1/4] Stopping old services..."
kill_matching_processes 'uvicorn.+apps\.api\.main:app'
kill_matching_processes 'apps\.worker\.feishu_worker'
kill_matching_processes 'apps\.worker\.scheduler'
kill_matching_processes 'vite.+127\.0\.0\.1.+5173'
kill_matching_processes 'admin-ui.+npm run dev'
sleep 2

echo ""
echo "[2/4] Clearing runtime data..."
rm -f data/db/app.sqlite data/db/*.log 2>/dev/null || true
rm -rf data/sessions/* 2>/dev/null || true
rm -rf data/audit/* 2>/dev/null || true
rm -rf data/reports/* 2>/dev/null || true
rm -f "$LOG_DIR"/*.log 2>/dev/null || true
echo "  Cleared database, sessions, audit logs, reports, and prior run logs"

echo ""
echo "[3/4] Initializing database..."
python scripts/init_db.py
python scripts/migrate_pipeline.py
python scripts/migrate_agent_session.py
python scripts/migrate_agent_runtime.py
python scripts/migrate_thread.py
python scripts/migrate_task_context.py
python scripts/migrate_app_settings.py
echo "  Database ready"

echo ""
echo "[4/4] Starting services..."
python -u -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8000 > "$LOG_DIR/api.log" 2>&1 &
API_PID=$!
echo "  API started (PID $API_PID)"

python -u -m apps.worker.feishu_worker > "$LOG_DIR/worker.log" 2>&1 &
WORKER_PID=$!
echo "  Feishu worker started (PID $WORKER_PID)"

python -u -m apps.worker.scheduler > "$LOG_DIR/scheduler.log" 2>&1 &
SCHEDULER_PID=$!
echo "  Scheduler started (PID $SCHEDULER_PID)"

(
    cd apps/admin-ui
    "$NPM_BIN" run dev -- --host 127.0.0.1 --port 5173 > "$LOG_DIR/frontend.log" 2>&1
) &
FRONTEND_PID=$!
echo "  Admin UI started (PID $FRONTEND_PID)"

sleep 2
ensure_pid_alive "$API_PID" "API"
ensure_pid_alive "$WORKER_PID" "Feishu worker"
ensure_pid_alive "$SCHEDULER_PID" "Scheduler"
ensure_pid_alive "$FRONTEND_PID" "Admin UI"

wait_for_http "http://127.0.0.1:8000/health" "API"
wait_for_http "http://127.0.0.1:5173" "Admin UI"

echo ""
echo "=== All services running ==="
echo "  API:       http://127.0.0.1:8000"
echo "  Frontend:  http://127.0.0.1:5173"
echo "  Logs:      $LOG_DIR"
echo ""
if [[ "$NO_WAIT" -eq 1 ]]; then
    echo "Leaving services running (--no-wait)"
    trap - EXIT INT TERM
    exit 0
fi

echo "Press Ctrl+C to stop all started services"

while true; do
    ensure_pid_alive "$API_PID" "API"
    ensure_pid_alive "$WORKER_PID" "Feishu worker"
    ensure_pid_alive "$SCHEDULER_PID" "Scheduler"
    ensure_pid_alive "$FRONTEND_PID" "Admin UI"
    sleep 5
done
