#!/usr/bin/env bash
# Four services, at least five processes: a2a + skill-center + arag + Runtime Worker + API.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "缺少虚拟环境：请先创建 .venv 并 pip install -r requirements.txt"
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "缺少 curl；健康检查和 durable index job 轮询需要 curl"
  exit 1
fi

if [ ! -f .env ]; then
  echo "[run_all] 未找到 .env；使用真实环境变量与代码默认值"
fi

get_env() {
  "$PY" - "$1" <<'PY'
import os
import sys
from dotenv import dotenv_values

name = sys.argv[1]
value = os.environ.get(name)
if value is None:
    value = dotenv_values(".env").get(name)
if value is not None:
    print(value)
PY
}
DASHSCOPE_KEY="$(get_env DASHSCOPE_API_KEY)"
if [ -z "$DASHSCOPE_KEY" ] || [ "$DASHSCOPE_KEY" = "sk-***" ]; then
  echo "DASHSCOPE_API_KEY 仍为空或占位值；请编辑 .env 或 export 真实 key"
  exit 1
fi
unset DASHSCOPE_KEY
AGENT_PORT="$(get_env AGENT_PORT)"; AGENT_PORT="${AGENT_PORT:-8000}"
ARAG_PORT="$(get_env ARAG_PORT)"; ARAG_PORT="${ARAG_PORT:-8100}"
SKILL_PORT="$(get_env SKILL_CENTER_PORT)"; SKILL_PORT="${SKILL_PORT:-8200}"
A2A_PORT="$(get_env A2A_SERVICE_PORT)"; A2A_PORT="${A2A_PORT:-8300}"
RUNTIME_DB="$(get_env RUNTIME_DB_PATH)"; RUNTIME_DB="${RUNTIME_DB:-local_storage/runtime/runtime.db}"
WORKER_ID="$(get_env RUNTIME_WORKER_ID)"; WORKER_ID="${WORKER_ID:-runtime-worker-local}"

"$PY" -m uvicorn a2a_service.main:app --host 0.0.0.0 --port "${A2A_PORT}" &
A2A_PID=$!
"$PY" -m uvicorn skillcenter.main:app --host 0.0.0.0 --port "${SKILL_PORT}" &
SKILL_PID=$!
"$PY" -m uvicorn arag.main:app --host 0.0.0.0 --port "${ARAG_PORT}" &
ARAG_PID=$!

cleanup() {
  if [ "${CLEANUP_DONE:-false}" = true ]; then
    return
  fi
  CLEANUP_DONE=true
  echo
  echo "[run_all] stopping..."
  for PID in "${A2A_PID}" "${SKILL_PID}" "${ARAG_PID}" "${WORKER_PID:-}" "${AGENT_PID:-}"; do
    [ -n "$PID" ] && kill "$PID" 2>/dev/null || true
  done
  for PID in "${A2A_PID}" "${SKILL_PID}" "${ARAG_PID}" "${WORKER_PID:-}" "${AGENT_PID:-}"; do
    [ -n "$PID" ] && wait "$PID" 2>/dev/null || true
  done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_health() {
  local url="$1" name="$2"
  local attempt
  for ((attempt=1; attempt<=60; attempt++)); do
    if curl -fsS --connect-timeout 1 --max-time 2 "$url" >/dev/null 2>&1; then
      echo "[run_all] ${name} ready"
      return 0
    fi
    sleep 1
  done
  echo "[run_all] ${name} health check timeout"
  return 1
}

wait_health "http://127.0.0.1:${A2A_PORT}/.well-known/agent-card.json" "a2a_service"
wait_health "http://127.0.0.1:${SKILL_PORT}/healthz" "skill-center"
wait_health "http://127.0.0.1:${ARAG_PORT}/healthz" "arag"

# Worker loads LLM/tools and registers all three immutable engine releases first.
WORKER_LAUNCHED_AT="$("$PY" -c 'import time; print(time.time_ns() // 1_000_000)')"
"$PY" -m agent.runtime.worker.main &
WORKER_PID=$!
RELEASES_READY=false
for ((RELEASE_ATTEMPT=1; RELEASE_ATTEMPT<=60; RELEASE_ATTEMPT++)); do
  if ! kill -0 "${WORKER_PID}" 2>/dev/null; then
    echo "[run_all] Runtime Worker exited during startup"
    exit 1
  fi
  if "$PY" - "$RUNTIME_DB" "$WORKER_ID" "$WORKER_LAUNCHED_AT" <<'PY' >/dev/null 2>&1
import json
import sqlite3
import sys

with sqlite3.connect(sys.argv[1]) as conn:
    active = dict(conn.execute(
        "select engine,release_fingerprint from active_releases"
    ))
    worker = conn.execute(
        "select release_map_json,state,heartbeat_at from runtime_workers where worker_id=?",
        (sys.argv[2],),
    ).fetchone()
expected = {"plan_execute", "agent_loop", "native_loop"}
ready = (
    set(active) == expected
    and worker is not None
    and worker[1] == "ACTIVE"
    and worker[2] >= int(sys.argv[3])
    and json.loads(worker[0]) == active
)
raise SystemExit(0 if ready else 1)
PY
  then
    echo "[run_all] Runtime Worker heartbeat and releases ready"
    RELEASES_READY=true
    break
  fi
  sleep 1
done
if [ "$RELEASES_READY" != true ]; then
  echo "[run_all] Runtime Worker heartbeat/release registration timeout"
  exit 1
fi
if ! kill -0 "${WORKER_PID}" 2>/dev/null; then
  echo "[run_all] Runtime Worker exited after readiness"
  exit 1
fi

# API never loads LLM or remote tool catalogs.
"$PY" -m uvicorn agent.main:app --host 0.0.0.0 --port "${AGENT_PORT}" &
AGENT_PID=$!
wait_health "http://127.0.0.1:${AGENT_PORT}/healthz" "agent-runtime-api"

echo "[run_all] seeding sample knowledge base..."
if ! SEED_RESPONSE="$(curl -sS --connect-timeout 2 --max-time 10 -w $'\n%{http_code}' -X POST "http://127.0.0.1:${ARAG_PORT}/v1/index/sample")"; then
  echo "[run_all] seed submission failed (检查 DASHSCOPE_API_KEY)"
  exit 1
fi
SEED_STATUS="${SEED_RESPONSE##*$'\n'}"
SEED_JSON="${SEED_RESPONSE%$'\n'*}"
if [ "$SEED_STATUS" != "202" ]; then
  echo "[run_all] seed submission must return HTTP 202 (got ${SEED_STATUS})"
  exit 1
fi
if ! JOB_ID_LIST="$("$PY" -c '
import json, sys
job_ids = json.load(sys.stdin).get("job_ids")
if not isinstance(job_ids, list) or not job_ids or not all(isinstance(item, str) and item for item in job_ids):
    raise SystemExit("sample index response has no valid job_ids")
print(*job_ids, sep="\n")
' <<<"$SEED_JSON")"; then
  echo "[run_all] invalid sample index response"
  exit 1
fi

# Bash 3.2 compatible replacement for mapfile (the default shell on macOS).
JOB_IDS=()
while IFS= read -r JOB_ID; do
  [ -n "$JOB_ID" ] && JOB_IDS+=("$JOB_ID")
done <<<"$JOB_ID_LIST"

for JOB_ID in "${JOB_IDS[@]}"; do
  JOB_ACTIVATED=false
  for ((JOB_ATTEMPT=1; JOB_ATTEMPT<=240; JOB_ATTEMPT++)); do
    JOB_JSON="$(curl -fsS --connect-timeout 1 --max-time 2 "http://127.0.0.1:${ARAG_PORT}/v1/index/jobs/${JOB_ID}" 2>/dev/null || true)"
    JOB_STATE="$("$PY" -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' <<<"$JOB_JSON" 2>/dev/null || true)"
    if [ "$JOB_STATE" = "ACTIVATED" ]; then
      JOB_ACTIVATED=true
      echo "[run_all] seed job ${JOB_ID} activated"
      break
    fi
    if [ "$JOB_STATE" = "FAILED" ]; then
      echo "[run_all] seed job ${JOB_ID} failed"
      exit 1
    fi
    sleep 0.25
  done
  if [ "$JOB_ACTIVATED" != true ]; then
    echo "[run_all] seed job ${JOB_ID} activation timeout"
    exit 1
  fi
done
echo "[run_all] all sample index jobs activated"

echo "[run_all] agent API → :${AGENT_PORT} | worker → local SQLite | arag → :${ARAG_PORT} | skill-center → :${SKILL_PORT} | a2a → :${A2A_PORT}"
echo "[run_all] open http://127.0.0.1:${AGENT_PORT}/chat-ui/"

# Bash 3.2 has no `wait -n`; polling keeps all five processes supervised.
while :; do
  for CHILD in \
    "a2a_service:${A2A_PID}" \
    "skill-center:${SKILL_PID}" \
    "arag:${ARAG_PID}" \
    "runtime-worker:${WORKER_PID}" \
    "runtime-api:${AGENT_PID}"; do
    CHILD_NAME="${CHILD%%:*}"
    CHILD_PID="${CHILD##*:}"
    if ! kill -0 "$CHILD_PID" 2>/dev/null; then
      CHILD_STATUS=0
      wait "$CHILD_PID" || CHILD_STATUS=$?
      echo "[run_all] ${CHILD_NAME} exited unexpectedly (status=${CHILD_STATUS})"
      exit 1
    fi
  done
  sleep 1
done
