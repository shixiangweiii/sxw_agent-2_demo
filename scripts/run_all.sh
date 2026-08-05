#!/usr/bin/env bash
# 一键启动 a2a_service(:8300) + skill-center(:8200) + arag(:8100) + agent(:8000)，等待健康检查并入库样本；Ctrl-C 同时退出。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "缺少虚拟环境：请先创建 .venv 并 pip install -r requirements.txt"
  exit 1
fi

[ -f .env ] || { echo "缺少 .env，请先 cp .env.example .env 并填入 DASHSCOPE_API_KEY"; exit 1; }

get_env() { grep -E "^$1=" .env | cut -d= -f2 || true; }
AGENT_PORT="$(get_env AGENT_PORT)"; AGENT_PORT="${AGENT_PORT:-8000}"
ARAG_PORT="$(get_env ARAG_PORT)"; ARAG_PORT="${ARAG_PORT:-8100}"
SKILL_PORT="$(get_env SKILL_CENTER_PORT)"; SKILL_PORT="${SKILL_PORT:-8200}"
A2A_PORT="$(get_env A2A_SERVICE_PORT)"; A2A_PORT="${A2A_PORT:-8300}"

# 下游先起：a2a_service / skill-center / arag（agent 启动时会发现 A2A、拉技能目录、检索）
"$PY" -m uvicorn a2a_service.main:app --host 0.0.0.0 --port "${A2A_PORT}" &
A2A_PID=$!
"$PY" -m uvicorn skillcenter.main:app --host 0.0.0.0 --port "${SKILL_PORT}" &
SKILL_PID=$!
"$PY" -m uvicorn arag.main:app --host 0.0.0.0 --port "${ARAG_PORT}" &
ARAG_PID=$!

cleanup() { echo; echo "[run_all] stopping..."; kill "${A2A_PID}" "${SKILL_PID}" "${ARAG_PID}" "${AGENT_PID:-}" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

wait_health() {
  local url="$1" name="$2"
  for _ in $(seq 1 30); do
    if curl -sf "$url" >/dev/null 2>&1; then echo "[run_all] ${name} ready"; return 0; fi
    sleep 1
  done
  echo "[run_all] ${name} health check timeout"; return 1
}
wait_health "http://127.0.0.1:${A2A_PORT}/.well-known/agent-card.json" "a2a_service"
wait_health "http://127.0.0.1:${SKILL_PORT}/healthz" "skill-center"
wait_health "http://127.0.0.1:${ARAG_PORT}/healthz" "arag"

# agent 后起：拉技能目录 + 加载 claude-skill + 发现 A2A 子代理
"$PY" -m uvicorn agent.main:app --host 0.0.0.0 --port "${AGENT_PORT}" &
AGENT_PID=$!
wait_health "http://127.0.0.1:${AGENT_PORT}/healthz" "agent"

echo "[run_all] seeding sample knowledge base..."
curl -sf -X POST "http://127.0.0.1:${ARAG_PORT}/v1/index/sample" >/dev/null && echo "[run_all] seeded" || echo "[run_all] seed failed (检查 DASHSCOPE_API_KEY)"

echo "[run_all] agent → :${AGENT_PORT} | arag → :${ARAG_PORT} | skill-center → :${SKILL_PORT} | a2a → :${A2A_PORT}"
echo "[run_all] try: curl -N -X POST http://127.0.0.1:${AGENT_PORT}/api/v1/chat/demo/stream -F 'query=用数据分析技能算 12,7,9,20 的均值方差' -F user_id=u1 -F session_id=s1"
wait
