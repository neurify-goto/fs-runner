#!/usr/bin/env bash
set -euo pipefail

LOG_FILE=${1:-}
if [[ -z "${LOG_FILE}" || ! -f "${LOG_FILE}" ]]; then
  echo "Usage: $0 /path/to/mapping_test.log" >&2
  exit 2
fi

# Extract most recent paths from the log (grep失敗を許容してから空判定)
# pipefail有効下でも早期終了しないように、パイプライン全体に || true を付与
MAP_JSON=$( { grep -Eo '/Users/[^"]+/analysis_result_[0-9_]{15}\.json' "$LOG_FILE" | tail -n1; } || true )
PAGE_HTML=$( { grep -Eo '/Users/[^"]+/page_source_[0-9_]{15}\.html' "$LOG_FILE" | tail -n1; } || true )

if [[ -z "$MAP_JSON" || -z "$PAGE_HTML" ]]; then
  echo "Failed to extract file paths from $LOG_FILE" >&2
  exit 3
fi

python3 scripts/claude_eval.py "$MAP_JSON" "$PAGE_HTML"
