#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
backend_root="$task_root/skin-assistant-langchain-backend"
node_binary="${NODE_BINARY:-node}"
database_file="${FRONTEND_E2E_DATABASE_FILE:-/private/tmp/skin-assistant-frontend-e2e-$$.sqlite3}"
report_directory="$backend_root/eval/reports"
dataset_path="$backend_root/eval/golden_questions.json"

export APP_ENV=test
export RAG_EMBEDDER=lexical
export CATALOG_SOURCE=mock
export DATABASE_URL="sqlite+pysqlite:///$database_file"
export EVALUATION_DETERMINISTIC_LOCAL=true
export FRONTEND_E2E_EVALUATION=true
export ENABLE_CART_DRAFTS=false
export MODEL_MAX_CALLS_PER_RUN=0
export UV_CACHE_DIR="${UV_CACHE_DIR:-/private/tmp/skin-assistant-uv-cache}"

run_browser_eval() {
  local confirmation_preview="$1"
  local output_path="$2"
  local backend_port="$3"
  local frontend_port="$4"
  shift 4
  export FRONTEND_E2E_CONFIRMATION_PREVIEW="$confirmation_preview"
  python3 /Users/mengfeng/.codex/skills/webapp-testing/scripts/with_server.py \
    --server "cd '$backend_root' && FRONTEND_E2E_CONFIRMATION_PREVIEW='$confirmation_preview' uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port $backend_port" --port "$backend_port" \
    --server "cd '$task_root' && VITE_API_TARGET=http://127.0.0.1:$backend_port VITE_USE_MOCK=false '$node_binary' node_modules/vite/bin/vite.js --host 127.0.0.1 --port $frontend_port --strictPort" --port "$frontend_port" \
    -- "$node_binary" "$backend_root/eval/frontend_e2e_report.mjs" \
      --base-url "http://127.0.0.1:$frontend_port" \
      --dataset "$dataset_path" \
      --output "$output_path" \
      "$@"
}

if (($# > 0)); then
  run_browser_eval "${FRONTEND_E2E_CONFIRMATION_PREVIEW:-false}" \
    "$report_directory/frontend_e2e_report.json" 8001 5175 "$@"
  exit 0
fi

normal_report="$report_directory/frontend_e2e_normal_report.json"
preview_report="$report_directory/frontend_e2e_confirmation_preview_report.json"
run_browser_eval false "$normal_report" 8001 5175 --exclude-evaluation-mode confirmation_preview

preview_case_ids="$($node_binary -e "const cases=require(process.argv[1]); console.log(cases.filter((item)=>item.evaluation_mode==='confirmation_preview').map((item)=>item.id).join(','))" "$dataset_path")"
run_browser_eval true "$preview_report" 8002 5176 --case-id "$preview_case_ids"

"$node_binary" -e "
const fs = require('node:fs');
const dataset = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
const reports = process.argv.slice(2).map((file) => JSON.parse(fs.readFileSync(file, 'utf8')));
const cases = new Map(reports.flatMap((report) => report.cases.map((item) => [item.case_id, item])));
const ordered = dataset.map((item) => cases.get(item.id)).filter(Boolean);
const report = {
  generated_at: new Date().toISOString(),
  execution_mode: 'frontend_e2e_deterministic_local_no_external_model_no_transaction_write',
  dataset: 'golden_questions.json',
  summary: { total_cases: ordered.length, passed_cases: ordered.filter((item) => item.passed).length, failed_cases: ordered.filter((item) => !item.passed).length },
  cases: ordered,
};
fs.writeFileSync(process.argv[4], JSON.stringify(report, null, 2) + '\\n');
console.log(JSON.stringify(report.summary));
process.exitCode = report.summary.failed_cases > 0 ? 1 : 0;
" "$dataset_path" "$normal_report" "$preview_report" "$report_directory/frontend_e2e_report.json"
