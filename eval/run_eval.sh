#!/usr/bin/env bash
# 一键评测：主 pass（两引擎）+ 报告。arag-down 鲁棒性 pass 因需停服，单独手动执行（见末尾提示）。
# 前置：DASHSCOPE_API_KEY 经环境变量注入（勿写文件）；两个 agent 实例已起（8000=agent_loop / 8001=plan_execute）；
#       下游 arag/skill-center/a2a 在线；样本知识已入库；多模态图已下载到 eval/dataset/assets/。
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DASHSCOPE_API_KEY:?需先 export DASHSCOPE_API_KEY（仅环境变量，勿写文件）}"
if [ -x env_sxw_demo/bin/python ]; then
  PY=env_sxw_demo/bin/python
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  echo "缺少虚拟环境：请先创建 .venv（或兼容旧路径 env_sxw_demo）并 pip install -r requirements.txt"
  exit 1
fi
LOOP_URL="${LOOP_URL:-http://127.0.0.1:8000}"
PLAN_URL="${PLAN_URL:-http://127.0.0.1:8001}"
DIR="eval/reports/$(date +%Y%m%d-%H%M%S)"
echo "[run_eval] out=$DIR"

"$PY" -m eval.harness.runner --engine agent_loop   --base-url "$LOOP_URL" --out "$DIR"
"$PY" -m eval.harness.runner --engine plan_execute --base-url "$PLAN_URL" --out "$DIR"
"$PY" -m eval.harness.report --out "$DIR"

echo "[run_eval] 主 pass 完成 → $DIR/summary.md"
echo "[run_eval] arag-down 鲁棒性 pass（需先停 arag）："
echo "  kill \$(lsof -ti tcp:8100)"
echo "  $PY -m eval.harness.runner --engine agent_loop   --base-url $LOOP_URL --out $DIR --only-arag-down"
echo "  $PY -m eval.harness.runner --engine plan_execute --base-url $PLAN_URL --out $DIR --only-arag-down"
echo "  $PY -m eval.harness.report --out $DIR   # 重新聚合"
