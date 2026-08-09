"""结构化轨迹（trace）：Span 树 + JSONL 落盘 + 内存缓存。

与 `common/obs.py` 的分工：

- `obs.py` 回答**"发生了什么"**——一行一条日志，靠 `[Tag]` 前缀检索；
- 本模块回答**"为什么会这样"**——一次请求 = 一棵 Span 树：每轮模型看到了什么输入、
  调了什么工具、耗时与 token、循环为什么又转了一圈。评测拿到一条 FAIL 记录后，
  正是靠这棵树把"哪里错了"变成"为什么错"。

设计要点：

1. **trace_id 复用 `obs.py` 的 contextvar**，不新建——同一个 id 既贯穿日志也贯穿 span，
   而且 `TraceMiddleware` 已经支持客户端经 `x-trace-id` 传入（评测据此建立联查键）。
2. **父子关系用另一个 contextvar**。`asyncio.create_task` 复制 context 的语义，
   正好让并发投递的工具 span 自动挂在发起它的那一轮 turn 下面，
   不需要把 span 当参数穿透深调用栈（工具是被引擎在很深的栈里执行的）。
3. **span 关闭即追加一行 JSONL**，不是结束时整体写一个 JSON。请求被 kill 也留下
   可读的半截轨迹（"恢复优先于失败"）。代价是 root span 在最后一行，
   读侧靠 `parent_span_id` 重建树，并容忍缺 root 的残缺文件。
4. **标量与大块内容分开存**（`attributes` vs `payloads`）。前者永远保留，
   后者按 `payload_level` 处理——这样 API 才能把已落盘的 full 后验降级成 summary。

⚠️ 落盘是同步 I/O（单条 span 通常几 KB）。本项目是本机 demo，不为此引入 aiofiles；
真实生产应换成异步 sink 或独立 writer 协程。
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from common.obs import get_logger, get_trace_id, log_kv

logger = get_logger("common.trace")

# ── span kind ─────────────────────────────────────────────────────────────
# 取值刻意收窄成一张固定表：评测的失败归因要按 kind 过滤，
# 自由取值会让下游规则变成字符串猜谜。
KIND_REQUEST = "request"        # 一次对外请求（root）
KIND_ENGINE = "engine"          # 引擎整体
KIND_PLAN = "plan"              # plan_execute 的规划相
KIND_TURN = "turn"              # 循环的一轮
KIND_LLM = "llm"                # 一次模型调用
KIND_TOOL = "tool"              # 一次工具执行
KIND_RETRIEVAL = "retrieval"    # 知识检索
KIND_SUB_AGENT = "sub_agent"    # 子代理委派
KIND_SKILL = "skill"            # 技能 / 沙箱
KIND_COMPACT = "compact"        # 上下文压缩

STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

LEVEL_NONE = "none"
LEVEL_SUMMARY = "summary"
LEVEL_FULL = "full"

# payload 摘要里保留的正文头部长度（summary 级别）
_SUMMARY_HEAD_CHARS = 200
# 单个 span 最多记多少 event，防病态流把内存吃满
_MAX_EVENTS_PER_SPAN = 2000
# 内存里保留最近多少条 trace（供查询 API 免读盘）
_RING_CAPACITY = 64
# 列表接口单次最多扫多少个文件。控制台会轮询，目录长大后不能每次全读。
_LIST_SCAN_MAX_FILES = 200
# 文件摘要缓存条数（键含 mtime/size，文件被追加就自动失效）
_SUMMARY_CACHE_CAPACITY = 512

# 敏感 key：命中即整值替换。刻意**不匹配裸 token**——
# prompt_tokens / completion_tokens / max_tokens 都是要留的正常字段。
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|authorization|secret|password|passwd|credential"
    r"|access[_-]?token|auth[_-]?token|bearer)",
    re.IGNORECASE,
)
# data URL：多模态图片进 messages 后会常驻会话历史、每轮都带，
# 原样落盘会让单条多模态 trace 冲到数 MB（496KB 的 jpeg → 662KB base64 × 轮次）。
_DATA_URL_RE = re.compile(r"^data:([^;,]+);base64,", re.IGNORECASE)
# 文件名里允许出现的字符。trace_id 是客户端可控的（TraceMiddleware 无条件采纳
# x-trace-id 请求头），不做白名单净化就是一个目录穿越洞。
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")
_SEQ_IN_NAME_RE = re.compile(r"^\d{8}-\d{6}-(\d{4})-")
# 同一模式的剥离版：把 `{day}-{HHMMSS}-{seq}-` 前缀去掉，剩下的是进程名段。
_SEQ_PREFIX_RE = re.compile(r"^\d{8}-\d{6}-\d{4}-")

_current_span: contextvars.ContextVar[Optional["Span"]] = contextvars.ContextVar(
    "current_span", default=None,
)


# ── 数据模型 ───────────────────────────────────────────────────────────────

@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    kind: str
    start_ts: float
    end_ts: Optional[float] = None
    duration_ms: Optional[float] = None
    status: str = STATUS_OK
    # 标量属性：轮次 / 工具名 / token / 命中数 / transition…… 永远保留。
    attributes: dict[str, Any] = field(default_factory=dict)
    # 大块内容：模型输入、工具参数与结果…… 按 payload_level 处理。
    payloads: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _events_dropped: int = 0

    def set(self, **attributes: Any) -> "Span":
        """记标量属性（永远保留，不受 payload_level 影响）。

        同样过一遍脱敏：标量位置一样可能被塞进 key/URL，而 attributes
        是**任何 level 下都会落盘**的那部分，漏在这里就没有第二道闸了。
        """
        for key, value in attributes.items():
            if value is None:
                continue
            self.attributes[key] = (
                "***" if _SECRET_KEY_RE.search(key) else redact(value)
            )
        return self

    def set_payload(self, key: str, value: Any) -> "Span":
        """记大块内容（脱敏后按 payload_level 落盘）。"""
        if value is None:
            return self
        self.payloads[key] = redact(value)
        return self

    def incr(self, key: str, delta: int = 1) -> "Span":
        """累加一个计数属性（流式场景：逐块累计字符数 / 次数）。"""
        self.attributes[key] = int(self.attributes.get(key, 0)) + delta
        return self

    def append(self, key: str, value: Any) -> "Span":
        """往一个列表属性里追加（流式场景：逐个累积 tool_call 名）。"""
        self.attributes.setdefault(key, []).append(redact(value))
        return self

    def set_status(self, status: str) -> "Span":
        """标记状态（供回调式埋点：先标错、稍后再由配对回调统一收口）。"""
        self.status = status
        return self

    def add_event(self, name: str, **fields: Any) -> "Span":
        """记一个时间点（如一帧 SSE 事件）。"""
        if len(self.events) >= _MAX_EVENTS_PER_SPAN:
            self._events_dropped += 1
            return self
        self.events.append({
            "name": name,
            "ts": time.time(),
            **{k: redact(v) for k, v in fields.items() if v is not None},
        })
        return self

    def to_dict(self, level: str = LEVEL_FULL) -> dict[str, Any]:
        out: dict[str, Any] = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "start_ts": round(self.start_ts, 6),
            "end_ts": round(self.end_ts, 6) if self.end_ts else None,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
        }
        if self.payloads and level != LEVEL_NONE:
            out["payloads"] = (
                self.payloads if level == LEVEL_FULL
                else {k: _digest(v) for k, v in self.payloads.items()}
            )
        if self.events:
            out["events"] = self.events
        if self._events_dropped:
            out["events_dropped"] = self._events_dropped
        return out


class _NullSpan:
    """tracing 关闭时的空实现，让调用方不必到处写 if。

    ``status`` 是类属性且 ``set_status`` 不写回：本对象是进程级单例，
    真写进去会让状态跨请求残留。
    """

    span_id = ""
    status = STATUS_OK

    def set(self, **_: Any) -> "_NullSpan":
        return self

    def incr(self, *_: Any, **__: Any) -> "_NullSpan":
        return self

    def append(self, *_: Any, **__: Any) -> "_NullSpan":
        return self

    def set_status(self, _: str) -> "_NullSpan":
        return self

    def set_payload(self, *_: Any, **__: Any) -> "_NullSpan":
        return self

    def add_event(self, *_: Any, **__: Any) -> "_NullSpan":
        return self


_NULL_SPAN = _NullSpan()


# ── 脱敏与裁剪 ─────────────────────────────────────────────────────────────

def _sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()[:12]


def _digest(value: Any) -> dict[str, Any]:
    """把任意 payload 压成 {chars, sha1, head} —— summary 级别的表示。"""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str,
    )
    return {
        "chars": len(text),
        "sha1": _sha1(text.encode("utf-8", "replace")),
        "head": text[:_SUMMARY_HEAD_CHARS],
    }


def redact(value: Any, _depth: int = 0) -> Any:
    """脱敏 + 裁剪 + 转成可 JSON 序列化的形态。

    四条规则，按危害排序：

    1. **图片 / 二进制 → 占位摘要**。多模态 case 不处理就是单条 trace 数 MB。
       约定与 ``native_loop/compact.py::_render_content`` 一致：图片只留占位，
       下游（那边是摘要模型，这边是轨迹文件）都不该看到 base64。
    2. **敏感 key → ***"**。密钥纪律。
    3. **超长字符串 → 截断**。防单条巨型工具结果。
    4. pydantic / dataclass / 其它对象 → dict 或 str，保证可序列化。
    """
    if _depth > 12:                       # 防御性：环状或超深结构
        return "[max depth]"

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, (bytes, bytearray)):
        return f"[binary {len(value)}B sha1={_sha1(bytes(value))}]"

    if isinstance(value, str):
        match = _DATA_URL_RE.match(value)
        if match:
            return (f"[image {match.group(1)} {len(value)}B "
                    f"sha1={_sha1(value.encode('utf-8', 'replace'))}]")
        limit = _tracer.max_field_chars
        if len(value) > limit:
            return value[:limit] + f"…[truncated, {len(value)} chars total]"
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        artifact_backed = any(
            value.get(key)
            for key in ("artifact_ref", "result_ref", "evidence_set_ref")
        )
        for key, item in value.items():
            name = str(key)
            if _SECRET_KEY_RE.search(name):
                out[name] = "***"
            elif artifact_backed and name in {
                "content", "preview", "data", "response", "result",
            }:
                # The durable Artifact is the authority.  A trace retains its
                # ref plus a digest/size signal, never another copy of the model
                # slice or large ToolResult.
                out[name] = _artifact_payload_digest(item)
            else:
                out[name] = redact(item, _depth + 1)
        return out

    if isinstance(value, (list, tuple, set)):
        return [redact(item, _depth + 1) for item in value]

    # pydantic v2（ADK / genai 的 Content、Part、LlmRequest 都是）
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return redact(dump(exclude_none=True), _depth + 1)
        except Exception:  # noqa: BLE001 - 转换失败不该影响主流程
            pass
    if hasattr(value, "__dict__"):
        try:
            return redact(dict(vars(value)), _depth + 1)
        except Exception:  # noqa: BLE001
            pass
    return redact(str(value), _depth + 1)


def _artifact_payload_digest(value: Any) -> dict[str, Any]:
    try:
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, default=str, sort_keys=True,
        )
    except (TypeError, ValueError):
        text = str(value)
    return {
        "artifact_backed": True,
        "chars": len(text),
        "sha1": _sha1(text.encode("utf-8", "replace")),
    }


# ── 落盘 ───────────────────────────────────────────────────────────────────

@dataclass
class _TraceRecord:
    trace_id: str
    path: Optional[Path]
    spans: list[dict[str, Any]] = field(default_factory=list)


def _sanitize(trace_id: str) -> str:
    """trace_id → 可安全用作文件名的片段（白名单 + 截断）。"""
    safe = _UNSAFE_NAME_RE.sub("_", trace_id or "").strip("_")[:32]
    return safe or "trace"


class _Tracer:
    """进程级单例：持配置、序号、内存 ring buffer。"""

    def __init__(self) -> None:
        self.enabled = False
        self.payload_level = LEVEL_FULL
        self.max_field_chars = 20000
        self.engine = "unknown"
        self._dir: Optional[Path] = None
        self._ring: "OrderedDict[str, _TraceRecord]" = OrderedDict()
        self._seq_day = ""
        self._seq = 0

    # 每天首次写入时扫当日目录取 max 续号：抗进程重启（否则重启后序号归零、
    # 与既有文件撞名）。一天一次 O(n)，可忽略。
    #
    # ⚠️ seq 是「**本进程**当日第 N 次」，不是全局序号。API、Worker
    # 和下游进程各自记录；文件名里的 engine 段与 trace_id 段避免撞车。
    def _next_seq(self, day: str, day_dir: Path) -> int:
        if day != self._seq_day:
            self._seq_day = day
            self._seq = self._scan_max_seq(day_dir)
        self._seq += 1
        return self._seq

    @staticmethod
    def _scan_max_seq(day_dir: Path) -> int:
        largest = 0
        try:
            for item in day_dir.iterdir():
                match = _SEQ_IN_NAME_RE.match(item.name)
                if match:
                    largest = max(largest, int(match.group(1)))
        except OSError:
            return 0
        return largest

    def path_for(self, trace_id: str) -> Optional[Path]:
        if self._dir is None:
            return None
        now = datetime.now()
        day = now.strftime("%Y%m%d")
        day_dir = self._dir / day
        try:
            day_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        seq = self._next_seq(day, day_dir)
        name = (f"{day}-{now.strftime('%H%M%S')}-{seq:04d}"
                f"-{_sanitize(self.engine)}-{_sanitize(trace_id)}.jsonl")
        return day_dir / name

    def record(self, trace_id: str) -> _TraceRecord:
        rec = self._ring.get(trace_id)
        if rec is None:
            rec = _TraceRecord(trace_id=trace_id, path=self.path_for(trace_id))
            self._ring[trace_id] = rec
            while len(self._ring) > _RING_CAPACITY:
                self._ring.popitem(last=False)
        return rec

    def lookup(self, trace_id: str) -> Optional[_TraceRecord]:
        return self._ring.get(trace_id)

    def emit(self, span: Span) -> None:
        payload = span.to_dict(self.payload_level)
        rec = self.record(span.trace_id)
        rec.spans.append(payload)
        if rec.path is None:
            return
        try:
            with rec.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            # 轨迹写不进去绝不能影响对话本身，降级为一条警告日志。
            log_kv(logger, logging.WARNING, "Trace", "span write failed",
                   error=type(exc).__name__, path=str(rec.path))
            rec.path = None

    @property
    def dir(self) -> Optional[Path]:
        return self._dir

    def configure(
        self, *, enabled: bool, payload_level: str, trace_dir: str,
        max_field_chars: int, retention_days: int, engine: str,
    ) -> None:
        self.enabled = enabled
        self.payload_level = payload_level if payload_level in (
            LEVEL_NONE, LEVEL_SUMMARY, LEVEL_FULL) else LEVEL_FULL
        self.max_field_chars = max(200, max_field_chars)
        self.engine = engine
        self._ring.clear()
        self._seq_day, self._seq = "", 0
        if not enabled:
            self._dir = None
            return
        root = Path(trace_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_kv(logger, logging.WARNING, "Trace", "trace dir unavailable, memory only",
                   error=type(exc).__name__, dir=str(root))
            self._dir = None
            return
        self._dir = root
        _prune(root, retention_days)


_tracer = _Tracer()
# 文件摘要缓存：键是 (path, mtime_ns, size)，所以文件一被追加就自然失效。
_summary_cache: "OrderedDict[tuple[str, int, int], dict[str, Any]]" = OrderedDict()


def _prune(root: Path, retention_days: int) -> None:
    """删除过期的日期目录。按天分目录，所以清理就是 rmtree 一个目录。"""
    if retention_days <= 0:
        return
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y%m%d")
    removed = 0
    try:
        for item in root.iterdir():
            if item.is_dir() and len(item.name) == 8 and item.name.isdigit() and item.name < cutoff:
                shutil.rmtree(item, ignore_errors=True)
                removed += 1
    except OSError:
        return
    if removed:
        log_kv(logger, logging.INFO, "Trace", "pruned expired trace dirs",
               removed=removed, retention_days=retention_days)


# ── 对外 API ───────────────────────────────────────────────────────────────

def configure_tracing(
    *,
    enabled: bool = True,
    payload_level: str = LEVEL_FULL,
    trace_dir: str = "local_storage/traces",
    max_field_chars: int = 20000,
    retention_days: int = 7,
    engine: str = "unknown",
) -> None:
    """初始化 trace（形状对齐 `obs.setup_logging`：由服务入口调用，common 不反向依赖 agent.config）。"""
    _tracer.configure(
        enabled=enabled, payload_level=payload_level, trace_dir=trace_dir,
        max_field_chars=max_field_chars, retention_days=retention_days, engine=engine,
    )
    _summary_cache.clear()
    log_kv(logger, logging.INFO, "Trace", "tracing configured",
           enabled=enabled, payload_level=_tracer.payload_level,
           dir=str(_tracer.dir) if _tracer.dir else None,
           retention_days=retention_days, engine=engine)


def tracing_enabled() -> bool:
    return _tracer.enabled


def current_span() -> Optional[Span]:
    return _current_span.get()


@contextlib.contextmanager
def start_span(name: str, kind: str, **attributes: Any) -> Iterator[Any]:
    """开一个 span，退出时自动收口并落盘。

    父子关系取自 contextvar，所以在 ``asyncio.create_task`` 里开的 span
    会自动挂在创建该 task 时所处的 span 下——并发工具因此天然归属正确的 turn。
    """
    if not _tracer.enabled:
        yield _NULL_SPAN
        return

    parent = _current_span.get()
    span = Span(
        trace_id=get_trace_id(),
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=parent.span_id if parent else None,
        name=name,
        kind=kind,
        start_ts=time.time(),
    )
    span.set(**attributes)
    token = _current_span.set(span)
    try:
        yield span
    except (GeneratorExit, StopAsyncIteration):
        # 客户端断开时消费方 aclose() 抛来的是 GeneratorExit——它既不是 Exception
        # 也不是 CancelledError（本仓库在 native_loop/loop.py:214 已踩过这个坑）。
        # 漏了它，取消路径上的 span 会被记成 ok，或者干脆不收口。
        span.status = STATUS_CANCELLED
        raise
    except BaseException as exc:
        import asyncio  # noqa: PLC0415 - 仅异常路径需要
        span.status = (STATUS_CANCELLED if isinstance(exc, asyncio.CancelledError)
                       else STATUS_ERROR)
        if span.status == STATUS_ERROR:
            span.set(error_type=type(exc).__name__, error=str(exc)[:500])
        raise
    finally:
        _current_span.reset(token)
        span.end_ts = time.time()
        span.duration_ms = round((span.end_ts - span.start_ts) * 1000, 2)
        _tracer.emit(span)


def open_span(
    name: str, kind: str, parent: Any = None, **attributes: Any,
) -> Any:
    """手动开一个 span（配对 `close_span`），用于**回调式**埋点。

    ADK Plugin 的回调是一串离散的点（before_model / after_tool / …），
    跨回调没法用 `with`，只能手动配对。与 `start_span` 的关键差别：
    **本函数不碰 contextvar**——否则 set 了却没有对应的 reset 时机，
    会把当前 span 泄漏给同一 task 的后续无关逻辑。
    父 span 因此要显式传，缺省才回落到 contextvar。
    """
    if not _tracer.enabled:
        return _NULL_SPAN
    if parent is None:
        parent = _current_span.get()
    parent_id = getattr(parent, "span_id", None) or None
    span = Span(
        trace_id=get_trace_id(),
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=parent_id,
        name=name,
        kind=kind,
        start_ts=time.time(),
    )
    span.set(**attributes)
    return span


def push_span(span: Any) -> Any:
    """把一个 `open_span` 出来的 span 设为当前（配对 `pop_span`）。

    用于回调式埋点里"希望被埋点代码内部新建的 span 挂到我下面"的场景
    （典型：ADK 的 before_tool → 工具函数内部的 retrieval span）。
    调用方必须保证 push/pop 在**同一个 task** 里配对，否则 reset 会失败——
    `pop_span` 对此做了兜底，最坏结果只是树形略偏，绝不影响主流程。
    """
    if not isinstance(span, Span):
        return None
    return _current_span.set(span)


def pop_span(token: Any) -> None:
    if token is None:
        return
    try:
        _current_span.reset(token)
    except ValueError:
        # token 来自另一个 Context（回调被跨 task 调用）。此时不能 reset，
        # 只能退回无父状态，避免把一个已结束的 span 永久留作"当前"。
        _current_span.set(None)


def close_span(span: Any, status: str = STATUS_OK, **attributes: Any) -> None:
    """收口并落盘一个 `open_span` 出来的 span（对 None / 空实现是 no-op）。"""
    if not isinstance(span, Span):
        return
    span.set(**attributes)
    span.status = status
    span.end_ts = time.time()
    span.duration_ms = round((span.end_ts - span.start_ts) * 1000, 2)
    _tracer.emit(span)


def get_trace(trace_id: str, level: str = LEVEL_FULL) -> Optional[dict[str, Any]]:
    """取一条 trace（内存 ring + 全部同名落盘文件，按 span_id 去重后合并）。

    ``level`` 只能**降级**：落盘时若已是 summary，这里再要 full 也拿不回来。

    ⚠️ 一个 trace_id 可能对应**多个文件**：Worker 重启后重试同一个 Run，
    ring 已清空、会重新开一个文件，但 trace_id 取自持久化的 Run 因而不变。
    只读最新的那个会静默丢掉前一次 attempt 的全部 span，所以这里合并。
    内存记录与它自己的文件是同一批 span（`emit` 同时写两边），靠 span_id 去重。
    """
    merged: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    sources: list[str] = []

    rec = _tracer.lookup(trace_id)
    if rec is not None:
        sources.append("memory")
        for span in rec.spans:
            merged.setdefault(str(span.get("span_id")), span)

    paths = _find_trace_files(trace_id)
    if paths:
        sources.append("file")
        for path in paths:
            for span in _read_jsonl(path):
                merged.setdefault(str(span.get("span_id")), span)

    if not sources:
        return None
    spans = list(merged.values())
    if level != LEVEL_FULL:
        spans = [_downgrade(s, level) for s in spans]
    return {
        "trace_id": trace_id,
        "level": level,
        "source": "+".join(sources),
        "engine": _tracer.engine,
        # full 轨迹在本机的落盘位置。评测据此在报告里留一个指针而不是二次拷贝
        # ——前提是 harness 与 agent 同机（本项目本来就是本地跑）。
        "trace_files": [str(path) for path in paths],
        "span_count": len(spans),
        # span 按关闭顺序追加，所以 root 在最后；读侧统一按开始时间排，
        # 再靠 parent_span_id 重建树（文件可能因进程被 kill 而缺 root）。
        "spans": sorted(spans, key=lambda s: s.get("start_ts") or 0),
    }


def _downgrade(span: dict[str, Any], level: str) -> dict[str, Any]:
    payloads = span.get("payloads")
    if not payloads:
        return span
    out = dict(span)
    if level == LEVEL_NONE:
        out.pop("payloads", None)
    else:
        out["payloads"] = {k: _digest(v) for k, v in payloads.items()}
    return out


def _find_trace_files(trace_id: str) -> list[Path]:
    """按净化后的 trace_id 后缀在日期目录里找出**全部**文件（新的在前）。

    同一个 trace_id 跨 Worker 重启会产生多个文件，调用方要把它们当成一条轨迹。
    """
    root = _tracer.dir
    if root is None:
        return []
    pattern = f"*-{_sanitize(trace_id)}.jsonl"
    found: list[Path] = []
    try:
        days = sorted((d for d in root.iterdir() if d.is_dir()), reverse=True)
    except OSError:
        return []
    for day in days:
        found.extend(sorted(day.glob(pattern), reverse=True))
    return found


def _day_dirs() -> list[Path]:
    """日期目录，新→旧。"""
    root = _tracer.dir
    if root is None:
        return []
    try:
        return sorted(
            (d for d in root.iterdir()
             if d.is_dir() and len(d.name) == 8 and d.name.isdigit()),
            reverse=True,
        )
    except OSError:
        return []


def available_trace_days() -> list[str]:
    """有轨迹的日期（`YYYYMMDD`，新→旧），供控制台做日期选择。"""
    return [day.name for day in _day_dirs()]


def _file_summary(path: Path) -> Optional[dict[str, Any]]:
    """把一个轨迹文件压成摘要（按 mtime/size 记忆化，供列表轮询复用）。

    ⚠️ trace_id 取自**文件内容**而不是文件名：文件名是
    `{day}-{time}-{seq}-{process}-{trace_id}`，而 process 与 trace_id 都可能含
    `-`，切文件名无法可靠还原。反过来，知道了 trace_id 就能从文件名尾部剥出
    process 段（`runtime-worker` / `runtime-api`），那才是写这条轨迹的进程。
    """
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _summary_cache.get(key)
    if cached is not None:
        _summary_cache.move_to_end(key)
        return cached

    spans = _read_jsonl(path)
    if not spans:
        return None
    trace_id = str(spans[0].get("trace_id") or "")
    suffix = f"-{_sanitize(trace_id)}"
    process = path.stem[:-len(suffix)] if suffix != "-" and path.stem.endswith(suffix) else ""
    # 文件名前缀是 `{day}-{HHMMSS}-{seq:04d}-`，剥掉它剩下的才是进程名。
    process = _SEQ_PREFIX_RE.sub("", process)

    starts = [s.get("start_ts") for s in spans if s.get("start_ts") is not None]
    ends = [s.get("end_ts") for s in spans if s.get("end_ts") is not None]
    kinds: dict[str, int] = {}
    tools: list[str] = []
    tokens = 0
    errors = cancelled = 0
    has_root = False
    engine = run_id = release = None
    for span in spans:
        # 根 span 最后才关闭并落盘，所以"还没有根"= 这次 attempt 仍在跑（或进程被
        # kill 了）。用 parent_span_id 判定而不是看 engine span：重构前的轨迹根是
        # `chat.request`，只认 engine span 会把所有历史轨迹误标成进行中。
        if span.get("parent_span_id") is None:
            has_root = True
        kind = str(span.get("kind") or "")
        kinds[kind] = kinds.get(kind, 0) + 1
        attributes = span.get("attributes") or {}
        status = span.get("status")
        if status == STATUS_ERROR:
            errors += 1
        elif status == STATUS_CANCELLED:
            cancelled += 1
        if kind == KIND_LLM:
            tokens += int(attributes.get("total_tokens") or 0)
        elif kind == KIND_TOOL and attributes.get("tool"):
            tools.append(str(attributes["tool"]))
        elif kind == KIND_ENGINE:
            engine = attributes.get("engine") or engine
            run_id = attributes.get("run_id") or run_id
            release = attributes.get("release_fingerprint") or release

    summary = {
        "trace_id": trace_id,
        "trace_file": str(path),
        "process": process,
        "engine": engine,
        "run_id": run_id,
        "release_fingerprint": release,
        "started_at": min(starts) if starts else None,
        "ended_at": max(ends) if ends else None,
        "span_count": len(spans),
        "error_count": errors,
        "cancelled_count": cancelled,
        "kinds": kinds,
        "total_tokens": tokens or None,
        "tool_calls": tools,
        "has_root": has_root,
    }
    _summary_cache[key] = summary
    while len(_summary_cache) > _SUMMARY_CACHE_CAPACITY:
        _summary_cache.popitem(last=False)
    return summary


def _merge_summaries(items: list[dict[str, Any]]) -> dict[str, Any]:
    """把同一 trace_id 的多个文件摘要合并成一条（跨 Worker 重启的重试）。"""
    starts = [item["started_at"] for item in items if item["started_at"] is not None]
    ends = [item["ended_at"] for item in items if item["ended_at"] is not None]
    kinds: dict[str, int] = {}
    for item in items:
        for kind, count in item["kinds"].items():
            kinds[kind] = kinds.get(kind, 0) + count
    started = min(starts) if starts else None
    ended = max(ends) if ends else None
    errors = sum(item["error_count"] for item in items)
    cancelled = sum(item["cancelled_count"] for item in items)
    has_root = any(item["has_root"] for item in items)
    tokens = sum(item["total_tokens"] or 0 for item in items)
    return {
        "trace_id": items[0]["trace_id"],
        "trace_files": [item["trace_file"] for item in items],
        "process": next((item["process"] for item in items if item["process"]), ""),
        "engine": next((item["engine"] for item in items if item["engine"]), None),
        "run_id": next((item["run_id"] for item in items if item["run_id"]), None),
        "release_fingerprint": next(
            (item["release_fingerprint"] for item in items if item["release_fingerprint"]),
            None,
        ),
        "started_at": started,
        "duration_ms": (
            round((ended - started) * 1000, 2)
            if started is not None and ended is not None else None
        ),
        "span_count": sum(item["span_count"] for item in items),
        "attempts": len(items),
        "error_count": errors,
        "kinds": kinds,
        "total_tokens": tokens or None,
        "tool_calls": [tool for item in items for tool in item["tool_calls"]],
        "status": (
            STATUS_ERROR if errors
            else STATUS_CANCELLED if cancelled
            else STATUS_OK
        ),
        "in_flight": not has_root,
    }


def list_traces(
    *,
    day: Optional[str] = None,
    limit: int = 50,
    engine: Optional[str] = None,
    query: Optional[str] = None,
    status: Optional[str] = None,
) -> dict[str, Any]:
    """列出轨迹摘要（新→旧），供 Trace Console 浏览。

    只读文件系统，不碰 Run/Activity 权威表——Trace 是 Diagnostic，
    列表也不例外。扫描量受 `_LIST_SCAN_MAX_FILES` 约束，避免目录膨胀后
    每次轮询都把整个 traces 目录读一遍。
    """
    days = _day_dirs()
    day_names = [item.name for item in days]
    if day is not None:
        days = [item for item in days if item.name == day]

    scanned: list[dict[str, Any]] = []
    for day_dir in days:
        try:
            files = sorted(day_dir.glob("*.jsonl"), reverse=True)
        except OSError:
            continue
        for path in files:
            if len(scanned) >= _LIST_SCAN_MAX_FILES:
                break
            summary = _file_summary(path)
            if summary is not None:
                scanned.append(summary)
        if len(scanned) >= _LIST_SCAN_MAX_FILES:
            break

    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for item in scanned:
        grouped.setdefault(item["trace_id"], []).append(item)
    traces = [_merge_summaries(items) for items in grouped.values()]

    if engine:
        traces = [item for item in traces if item["engine"] == engine]
    if status:
        traces = [
            item for item in traces
            if (item["status"] == status
                or (status == "in_flight" and item["in_flight"]))
        ]
    if query:
        needle = query.lower()
        traces = [
            item for item in traces
            if needle in item["trace_id"].lower()
            or needle in str(item["run_id"] or "").lower()
        ]

    traces.sort(key=lambda item: item["started_at"] or 0, reverse=True)
    total = len(traces)
    return {
        "days": day_names,
        "day": day,
        "total": total,
        "truncated": len(scanned) >= _LIST_SCAN_MAX_FILES,
        "traces": traces[:max(1, limit)],
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    spans.append(json.loads(line))
                except json.JSONDecodeError:
                    continue          # 半截行（进程被 kill）跳过，不让整条 trace 不可读
    except OSError:
        return []
    return spans


__all__ = [
    "KIND_COMPACT", "KIND_ENGINE", "KIND_LLM", "KIND_PLAN", "KIND_REQUEST",
    "KIND_RETRIEVAL", "KIND_SKILL", "KIND_SUB_AGENT", "KIND_TOOL", "KIND_TURN",
    "LEVEL_FULL", "LEVEL_NONE", "LEVEL_SUMMARY",
    "STATUS_CANCELLED", "STATUS_ERROR", "STATUS_OK",
    "Span", "available_trace_days", "close_span", "configure_tracing", "current_span",
    "get_trace", "list_traces", "open_span", "pop_span", "push_span", "redact",
    "start_span", "tracing_enabled",
]
