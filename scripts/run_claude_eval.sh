#!/usr/bin/env bash
set -euo pipefail

LOG_FILE=${1:-}
if [[ -z "${LOG_FILE}" || ! -f "${LOG_FILE}" ]]; then
  echo "Usage: $0 /path/to/mapping_test.log" >&2
  exit 2
fi

# Extract most recent paths from the log（パス表記の差異に強い抽出）
# - /Users/... などの固定接頭辞に依存しない
# - 行末の直近一致を採用
# - set -euo pipefail 下でも早期終了しない
MAP_JSON=$( { \
  awk 'match($0,/[^[:space:]]*analysis_result_[0-9_]{15}\.json/){m=substr($0,RSTART,RLENGTH)} END{if(m) print m}' "$LOG_FILE"; \
} || true )
PAGE_HTML=$( { \
  awk 'match($0,/[^[:space:]]*page_source_[0-9_]{15}\.html/){m=substr($0,RSTART,RLENGTH)} END{if(m) print m}' "$LOG_FILE"; \
} || true )

# 互換: 旧パターン（/Users/..）でもう一度試行（念のため） - 失敗を許容
if [[ -z "$MAP_JSON" ]]; then
  MAP_JSON=$( { grep -Eo '/[^\"]+/analysis_result_[0-9_]{15}\.json' "$LOG_FILE" | tail -n1; } || true )
fi
if [[ -z "$PAGE_HTML" ]]; then
  PAGE_HTML=$( { grep -Eo '/[^\"]+/page_source_[0-9_]{15}\.html' "$LOG_FILE" | tail -n1; } || true )
fi

if [[ -z "$MAP_JSON" || -z "$PAGE_HTML" ]]; then
  echo "Failed to extract file paths from $LOG_FILE" >&2
  exit 3
fi

python3 scripts/claude_eval.py "$MAP_JSON" "$PAGE_HTML"
