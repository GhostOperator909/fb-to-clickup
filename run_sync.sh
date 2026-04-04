#!/usr/bin/env bash
# Daily sync runner — safe to call from cron or manually
# Usage: ./run_sync.sh [--date-preset yesterday|last_7d|last_30d]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Override date preset if passed as argument
if [[ "${1:-}" == "--date-preset" && -n "${2:-}" ]]; then
    export DATE_PRESET="$2"
fi

# Ensure venv exists
if [[ ! -d ".venv" ]]; then
    echo "Creating virtual environment…"
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

echo "Starting Meta → ClickUp sync at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# Sync Creative Process list (NAD+/GLP-1/Sermorelin health ads)
python executions/sync_engine.py
EXIT_CODE_1=$?

# Sync Mike's Playground list (golf/semaglutide proof-of-concept)
CLICKUP_LIST_URL="https://app.clickup.com/9013368526/v/li/${MIKES_LIST_ID:-901326715789}" \
TITLE_MATCH_FALLBACK=1 \
python executions/sync_engine.py
EXIT_CODE_2=$?

EXIT_CODE=$(( EXIT_CODE_1 > EXIT_CODE_2 ? EXIT_CODE_1 : EXIT_CODE_2 ))

if [[ $EXIT_CODE -eq 0 ]]; then
    echo "Sync completed successfully."
elif [[ $EXIT_CODE -eq 1 ]]; then
    echo "Sync completed with some field update errors — check logs/."
    exit 1
else
    echo "Sync failed with a fatal error — check logs/ for details."
    exit 2
fi
