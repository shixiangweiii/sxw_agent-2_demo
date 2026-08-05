# eval_docs 生成语料与评测集

本目录用于补充基础问答质量评测，语料面向当前 arag/agent 逻辑设计：Markdown 长文档、HTTP 图片链接、可控事实锚点、citation 黄金文档。

## 目录

- `corpus/`：10 篇 Markdown 原始语料，每篇至少 5 个章节、超过 1500 字符，并包含 Markdown 图片 URL。
- `generated_eval/index_payload.json`：可直接 POST 到 arag `/v1/index` 的入库 payload。
- `generated_eval/cases_generated_rag.jsonl`：兼容现有 eval harness 的 QA case，共 36 条。
- `generated_eval/qa_answer_key.json`：人工查看用答案要点。
- `public_datasets/`：公开中文评测集下载样例和 README。
- `public_dataset_survey.md`：公开数据集调研和适配建议。

## 建议入库命令

```bash
curl -X POST http://127.0.0.1:8100/v1/index \
  -H 'Content-Type: application/json' \
  --data-binary @eval/eval_docs/generated_eval/index_payload.json
```

## 建议评测命令

现有 harness 支持 `--dataset`，可在 agent/arags 服务启动并入库本语料后执行：

```bash
PY=.venv/bin/python
OUT=eval/reports/generated-rag-$(date +%Y%m%d-%H%M%S)
$PY -m eval.harness.runner --engine agent_loop --base-url http://127.0.0.1:8000 --dataset eval/eval_docs/generated_eval/cases_generated_rag.jsonl --out "$OUT"
$PY -m eval.harness.report --out "$OUT"
```

如需对比 `plan_execute`，仍需按原 RUNBOOK 启动第二个 agent 实例。
