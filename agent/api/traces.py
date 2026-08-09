"""轨迹查询出口：GET /api/v1/traces（列表）与 GET /api/v1/traces/{trace_id}（单条）。

两类消费者：

- 评测 harness：跑完一条 case 后按 trace_id 把结构化轨迹捞回来，落进
  `eval/reports/<ts>/traces/`，让 FAIL 记录能一路点到"当时模型看到了什么"；
- Trace Console（`/trace-ui`）：先列表浏览，再展开成瀑布图与 Span 树。

只读、无副作用。`level` 只能**降级**（落盘时若已是 summary，这里再要 full 也拿不回来）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from common.obs import get_logger, log_kv
from common.trace import (
    LEVEL_FULL,
    LEVEL_NONE,
    LEVEL_SUMMARY,
    get_trace,
    list_traces,
    tracing_enabled,
)

router = APIRouter(prefix="/api/v1/traces", tags=["traces"])
logger = get_logger("agent.traces")


def _require_tracing() -> None:
    if not tracing_enabled():
        raise HTTPException(status_code=503, detail="tracing 未启用（TRACE_ENABLED=false）")


@router.get("")
async def browse_traces(
    day: Optional[str] = Query(None, pattern=r"^\d{8}$"),
    limit: int = Query(50, ge=1, le=200),
    engine: Optional[str] = Query(None, max_length=64),
    q: Optional[str] = Query(None, max_length=128),
    status: Optional[str] = Query(None, max_length=16),
) -> dict[str, Any]:
    """按天列出轨迹摘要（新→旧），供控制台浏览。

    摘要来自扫描 trace 目录，不读 Run/Activity 权威表——Trace 是 Diagnostic，
    列表也不例外。同一 trace_id 的多个文件（跨 Worker 重启的重试）合并成一条。
    """
    _require_tracing()
    return list_traces(day=day, limit=limit, engine=engine, query=q, status=status)


@router.get("/{trace_id}")
async def read_trace(
    trace_id: str,
    level: str = Query(LEVEL_FULL, pattern=f"^({LEVEL_NONE}|{LEVEL_SUMMARY}|{LEVEL_FULL})$"),
) -> dict:
    """按 trace_id 取一条轨迹（先查内存 ring buffer，未命中再读盘）。

    trace_id 是客户端可控的，落盘查找路径在 `common.trace` 里统一做白名单净化，
    这里不需要（也不应该）自己再拼路径。
    """
    _require_tracing()
    trace = get_trace(trace_id, level)
    if trace is None:
        # 常见原因：agent 重启（内存 buffer 清空）且 TRACE_RETENTION_DAYS 已清理掉文件，
        # 或 trace_id 写错。给出可执行的提示，别让调用方去猜。
        log_kv(logger, logging.INFO, "Trace", "trace not found", trace_id=trace_id)
        raise HTTPException(status_code=404, detail=f"未找到轨迹 {trace_id}")
    return trace
