# Native Loop 上下文压缩（Context Compaction）全链路详解

本文档深入分析 `native_loop` 的上下文压缩机制：在 token 逼近窗口上限前用 LLM 做"结构化摘要"替换早期历史，并对 413 / 上下文超长提供反应式恢复。

整体设计对应 CC（Claude Code）的 `autoCompact.ts` / `compact.ts` / `prompt.ts`（合计约 2400 行），本仓库实现了其**最小完整版**——保留形状与关键取舍，砍掉与本项目无关的部分（microcompact、snip、context-collapse、post-compact 文件恢复）。

---

## 目录

- [1. 核心问题与总体思路](#1-核心问题与总体思路)
- [2. 关键源码位置索引](#2-关键源码位置索引)
- [3. 数据结构与配置](#3-数据结构与配置)
- [4. 触发判定阶段：`decide`](#4-触发判定阶段decide)
- [5. 执行压缩阶段：`compact`](#5-执行压缩阶段compact)
- [6. 主动压缩入口：`_maybe_proactive_compact`](#6-主动压缩入口_maybe_proactive_compact)
- [7. 压缩结果采用：`_adopt_compacted`](#7-压缩结果采用_adopt_compacted)
- [8. 反应式压缩：`_reactive_compact`](#8-反应式压缩_reactive_compact)
- [9. 冷却机制：`_enter_compact_cooldown`](#9-冷却机制_enter_compact_cooldown)
- [10. 时序图](#10-时序图)
- [11. 调用栈图（含源码定位）](#11-调用栈图含源码定位)
- [12. 关键不变量与诚实边界](#12-关键不变量与诚实边界)

---

## 1. 核心问题与总体思路

### 1.1 问题场景

长对话 / 多工具调用的 Agent 循环中，`state.messages` 会持续膨胀。当总 token 逼近模型上下文窗口上限时，下一次请求会被上游返回 `413 context_length_exceeded`，会话就此中断。

### 1.2 设计思路

采用 **CC 式两阶段防御**：

| 阶段 | 触发条件 | 处理 |
|---|---|---|
| **主动压缩**（proactive） | 估算 token ≥ `context_window_tokens - buffer_tokens` | 在请求发出前先摘要历史 |
| **反应式压缩**（reactive） | 模型返回上下文超长错误 | 压缩后重来一轮 |

压缩本身由一次**额外的 LLM 调用**完成：把早期历史渲染为结构化文本，让模型产出 7 段式摘要，再用这条摘要消息**整体替换**早期历史，保留最近若干个原子单元原样。

### 1.3 总体架构

```text
                          ┌─────────────────────────────┐
                          │  NativeLoop.run() 主循环    │
                          │  loop.py:137                │
                          └──────────────┬──────────────┘
                                         │ 每轮迭代开始
                                         ▼
                          ┌─────────────────────────────┐
                          │ _maybe_proactive_compact()  │  ← 主动压缩
                          │ loop.py:348                 │
                          └──────────────┬──────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 │                                               │
                 ▼                                               ▼
   ┌──────────────────────────┐                 ┌──────────────────────────┐
   │ compact.decide()         │                 │ 跳过（未达阈值 / 冷却中）│
   │ compact.py:109           │                 └──────────────────────────┘
   └──────────────┬───────────┘
                  │ should=True
                  ▼
   ┌──────────────────────────┐
   │ compact.compact()        │  ← 一次额外 LLM 调用
   │ compact.py:194           │
   └──────────────┬───────────┘
                  │ 返回 [摘要, *preserved]
                  ▼
   ┌──────────────────────────┐
   │ _adopt_compacted()       │  ← 替换 state.messages + 置空 last_usage
   │ loop.py:415              │
   └──────────────────────────┘
                  │
                  ▼
        继续正常的模型请求流程 ...

                  │
                  │  若模型报 413
                  ▼
   ┌──────────────────────────┐
   │ _reactive_compact()      │  ← 反应式压缩（兜底）
   │ loop.py:383              │
   └──────────────────────────┘
```

---

## 2. 关键源码位置索引

| 模块 | 文件路径 | 关键符号 |
|---|---|---|
| 主循环 | `agent/engine/native_loop/loop.py` | `NativeLoop.run`, `_maybe_proactive_compact`, `_reactive_compact`, `_adopt_compacted`, `_enter_compact_cooldown`, `LoopConfig`, `LoopState` |
| 压缩实现 | `agent/engine/native_loop/compact.py` | `decide`, `estimate_tokens`, `compact`, `render_history`, `_extract_summary`, `_preserved_start`, `_SUMMARY_SYSTEM` |
| 消息模型 | `agent/engine/native_loop/messages.py` | `Msg`, `ToolCall`, `Usage`, `Unit`, `atomic_units`, `messages_after_boundary`, `KIND_COMPACT_SUMMARY`, `CHARS_PER_TOKEN` |
| 轻量补全客户端 | `agent/llm/chat.py` | `AgentChatClient.complete`（摘要调用入口） |
| 配置注入 | `agent/engine/native_loop/engine.py` | 第 144-146 行：`context_window_tokens / compact_buffer_tokens / compact_preserve_units` 注入 LoopConfig |
| Checkpoint 持久化 | `agent/engine/native_loop/engine.py` | 第 220、259 行：`compact_cooldown` 随 LoopState 落 checkpoint / 从 checkpoint 恢复 |

---

## 3. 数据结构与配置

### 3.1 `LoopConfig` —— 压缩相关参数

`agent/engine/native_loop/loop.py:66-78`

```python
@dataclass
class LoopConfig:
    max_iters: int                      # 软收尾轮次（到达即 force-summary 劝停）
    hard_cap: int                       # 硬熔断轮次（= max_iters + 余量）
    max_tool_concurrency: int
    streaming_tool_exec: bool
    tool_result_max_chars: int
    context_window_tokens: int          # 上下文压缩：有效窗口
    compact_buffer_tokens: int          # 上下文压缩：预留 buffer（阈值 = 窗口 − buffer）
    compact_preserve_units: int         # 压缩后原样保留的尾部原子单元数
    max_reactive_compacts: int = 1      # 反应式压缩最多尝试几次，默认 1
```

**阈值计算**：`threshold = context_window_tokens - compact_buffer_tokens`
（CC 用 13k buffer；本项目由配置决定，目的是让压缩在撞上 413 之前主动触发。）

### 3.2 `LoopState` —— 压缩相关可变状态

`agent/engine/native_loop/loop.py:86-98`

```python
@dataclass
class LoopState:
    messages: list[Msg]
    iters: int = 0
    transition: Optional[str] = None
    attempted_reactive_compact: int = 0   # 反应式压缩已尝试次数
    compact_failures: int = 0             # 压缩失败次数（仅用于可观测）
    compact_cooldown: int = 0             # >0 时跳过主动压缩，每轮递减
    last_usage: Optional[Usage] = None    # 上次模型请求的 prompt_tokens
    tool_state: dict[str, Any] = field(default_factory=dict)
```

### 3.3 压缩失败冷却常量

`agent/engine/native_loop/loop.py:81-83`

```python
# 压缩失败后跳过多少轮再重试。用冷却而不是"永久关闭"：一次偶发失败
# （比如摘要模型抖动）不该让长会话在剩余时间里彻底失去压缩能力。
_COMPACT_COOLDOWN_TURNS = 3
```

### 3.4 `Msg` 与 `KIND_COMPACT_SUMMARY`

`agent/engine/native_loop/messages.py:18-47`

```python
KIND_NORMAL = "normal"
KIND_COMPACT_SUMMARY = "compact_summary"   # 压缩摘要，同时充当 compact boundary
KIND_META = "meta"

@dataclass
class Msg:
    role: str                              # system | user | assistant | tool
    content: Any = None                    # str | list[block] | None
    tool_calls: Optional[list[ToolCall]] = None
    tool_call_id: Optional[str] = None     # role=tool 时必填
    name: Optional[str] = None             # role=tool 时记工具名
    is_error: bool = False                 # 本地标记，不进线格式
    kind: str = KIND_NORMAL                # 本地标记，不进线格式
```

### 3.5 `Unit` 与 `atomic_units`

`agent/engine/native_loop/messages.py:148-177`

```python
# 字符 → token 的粗略换算比（中文取值，偏保守，宁早压不晚压）
CHARS_PER_TOKEN = 1.5

@dataclass
class Unit:
    start: int     # 闭区间起点
    end: int       # 闭区间终点

def atomic_units(messages: list[Msg]) -> list[Unit]:
    """把消息列表切成一串连续的不可拆分单元。"""
    units: list[Unit] = []
    index = 0
    total = len(messages)
    while index < total:
        msg = messages[index]
        if msg.role == "assistant" and msg.tool_calls:
            pending = {tc.id for tc in msg.tool_calls}
            end = index
            probe = index + 1
            # 只吞紧随其后的 tool 消息；遇到非 tool 消息立即停止
            while probe < total and messages[probe].role == "tool":
                if messages[probe].tool_call_id in pending:
                    pending.discard(messages[probe].tool_call_id)
                end = probe
                probe += 1
            units.append(Unit(index, end))
            index = end + 1
        else:
            units.append(Unit(index, index))
            index += 1
    return units
```

**关键语义**：一条带 `tool_calls` 的 assistant 消息 + 紧随其后、引用其 call id 的所有 tool 消息，构成**不可拆分单元**。任何裁剪/压缩都必须在单元边界进行，否则 OpenAI 协议会直接 400。

---

## 4. 触发判定阶段：`decide`

### 4.1 入口：`compact.decide`

`agent/engine/native_loop/compact.py:109-123`

```python
def decide(
    messages: list[Msg],
    last_usage: Optional[Usage],
    *,
    context_window_tokens: int,
    buffer_tokens: int,
    fixed_overhead_chars: int = 0,
) -> CompactDecision:
    tokens, estimated = estimate_tokens(
        messages, last_usage, fixed_overhead_chars=fixed_overhead_chars,
    )
    threshold = max(1, context_window_tokens - buffer_tokens)
    return CompactDecision(
        should=tokens >= threshold, tokens=tokens, threshold=threshold, estimated=estimated,
    )
```

### 4.2 Token 估算：`estimate_tokens`

`agent/engine/native_loop/compact.py:71-97`

```python
def estimate_tokens(
    messages: list[Msg],
    last_usage: Optional[Usage],
    *,
    fixed_overhead_chars: int = 0,
) -> tuple[int, bool]:
    char_estimate = _char_tokens(messages, fixed_overhead_chars)
    if last_usage is not None and last_usage.prompt_tokens:
        # prompt_tokens 是"上一次请求"的输入规模；本轮又追加了 assistant + tool 消息，
        # 取二者较大者作为保守估计。
        return max(int(last_usage.prompt_tokens), char_estimate), False
    return char_estimate, True
```

**要点**：
- 优先用上游真实 `prompt_tokens`，否则按字符估算。
- 取 `max` 是因为历史在追加，旧 usage 可能低估当前。
- `fixed_overhead_chars` 是 system 指令 + 工具 JSON schema（不在 messages 里，但每轮都进请求），不加会系统性低估约 1.8k token。
- `estimated=True` 必须在日志如实标注，避免误判为精确 token 计数。

### 4.3 字符估算：`_char_tokens`

`agent/engine/native_loop/compact.py:100-106`

```python
def _char_tokens(messages: list[Msg], fixed_overhead_chars: int = 0) -> int:
    total = fixed_overhead_chars
    for msg in messages:
        total += len(_render_content(msg.content))
        for call in msg.tool_calls or []:
            total += len(call.name) + len(call.arguments or "")
    return int(total / _CHARS_PER_TOKEN)     # CHARS_PER_TOKEN = 1.5
```

---

## 5. 执行压缩阶段：`compact`

`agent/engine/native_loop/compact.py:194-243`

```python
async def compact(
    messages: list[Msg],
    chat: AgentChatClient,
    *,
    preserve_units: int,
    trigger: str,
) -> Optional[list[Msg]]:
    """执行一次压缩。成功返回新的消息列表，失败返回 None。"""
    start = _preserved_start(messages, preserve_units)
    to_summarize = messages[:start]
    preserved = messages[start:]
    if not to_summarize:
        # 单轮内容本身就超限，压缩帮不上忙
        log_kv(logger, logging.WARNING, "Compact", "nothing to summarize, skip",
               trigger=trigger, total=len(messages), preserve_units=preserve_units)
        return None

    rendered = render_history(to_summarize)
    if not rendered.strip():
        return None

    try:
        raw = await chat.complete(
            system=_SUMMARY_SYSTEM,
            user=f"请压缩以下对话历史：\n\n{rendered}",
            max_tokens=_SUMMARY_MAX_TOKENS,     # 4096，覆盖默认 512
            temperature=0.1,
        )
    except Exception as exc:
        log_kv(logger, logging.ERROR, "Compact", "summarization failed",
               trigger=trigger, error=type(exc).__name__)
        return None

    summary = _extract_summary(raw)
    if not summary:
        log_kv(logger, logging.WARNING, "Compact", "empty summary, skip", trigger=trigger)
        return None

    boundary = Msg(
        role="user",
        content=_SUMMARY_PREFIX + summary,
        kind=KIND_COMPACT_SUMMARY,
    )
    return [boundary, *preserved]
```

### 5.1 保留尾部起点：`_preserved_start`

`agent/engine/native_loop/compact.py:182-191`

```python
def _preserved_start(messages: list[Msg], preserve_units: int) -> int:
    """必须落在原子单元边界上：从 call/response 区间中间切开会让上游判 400。"""
    units: list[Unit] = atomic_units(messages)
    if len(units) <= preserve_units:
        return units[0].start if units else 0
    return units[len(units) - preserve_units].start
```

### 5.2 历史渲染：`render_history`

`agent/engine/native_loop/compact.py:143-166`

```python
_HISTORY_RENDER_MAX_CHARS = 60_000        # 喂给摘要模型的历史文本上限
_PER_MESSAGE_RENDER_MAX_CHARS = 2_000     # 单条消息截断长度

def render_history(messages: list[Msg]) -> str:
    lines: list[str] = []
    for msg in messages:
        if msg.role == "system":
            continue
        text = _render_content(msg.content)
        if len(text) > _PER_MESSAGE_RENDER_MAX_CHARS:
            text = text[:_PER_MESSAGE_RENDER_MAX_CHARS] + " …（本条已截断）"
        if msg.role == "assistant" and msg.tool_calls:
            calls = "；".join(
                f"{c.name}({(c.arguments or '')[:200]})" for c in msg.tool_calls
            )
            lines.append(f"[助手] {text}\n[发起工具调用] {calls}")
        elif msg.role == "tool":
            flag = "失败" if msg.is_error else "成功"
            lines.append(f"[工具结果 {msg.name} {flag}] {text}")
        else:
            lines.append(f"[{'用户' if msg.role == 'user' else '助手'}] {text}")
    rendered = "\n\n".join(lines)
    if len(rendered) > _HISTORY_RENDER_MAX_CHARS:
        # 头部最旧的内容优先丢：摘要的价值主要在近期上下文
        rendered = "…（更早的历史因过长未纳入摘要）\n\n" + rendered[-_HISTORY_RENDER_MAX_CHARS:]
    return rendered
```

**关键限制**：
- 多模态 `image_url` 替换为 `[图片]` 占位，摘要模型不需要看 base64。
- 单条超 2k 字符截断；整段超 60k 字符只保留尾部 60k，避免"为压缩而撑爆摘要请求"。
- 丢弃顺序：**越早越先丢**。

### 5.3 摘要系统指令：`_SUMMARY_SYSTEM`

`agent/engine/native_loop/compact.py:45-58`

```python
_SUMMARY_SYSTEM = (
    "你是对话历史压缩器。你的输出会**替换**掉这段历史，成为后续推理唯一能看到的上下文，"
    "因此必须完整保留继续完成任务所需的全部信息，宁可冗长也不要遗漏关键事实。\n"
    "先在 <analysis> 标签内梳理思路，再在 <summary> 标签内按下列七段输出：\n"
    "1. 用户的主要请求与意图：完整列出用户提出的所有请求，不要合并或概括掉细节。\n"
    "2. 关键概念与术语：本次对话涉及的领域概念、专有名词、约束条件。\n"
    "3. 已调用的工具与得到的结论：逐项列出调用过哪些工具、关键参数、返回的关键事实"
    "（尤其是检索到的资料内容与其序号，后续引用要用到）。\n"
    "4. 遇到的错误与修复：出现过的失败及其解决方式，特别是用户提出的纠正意见。\n"
    "5. 未完成的待办：用户明确要求但尚未完成的事项。\n"
    "6. 当前正在进行的工作：紧接这段历史之前正在做的事，尽可能具体。\n"
    "7. 下一步：仅当与用户最近一次明确请求直接相关时才写；否则写\"无\"。\n"
    "不要臆造历史中不存在的信息。"
)

_SUMMARY_PREFIX = "[早期对话已被压缩，以下是完整摘要，请把它当作已发生的事实继续推进任务]\n\n"
_SUMMARY_MAX_TOKENS = 4096   # 摘要要覆盖完整、不遗漏，默认 512 会被截断
```

### 5.4 抽取摘要：`_extract_summary`

`agent/engine/native_loop/compact.py:169-179`

```python
def _extract_summary(raw: str) -> str:
    """取 <summary> 段；模型没按格式输出时退回全文（宁可多留，不要丢信息）。"""
    lowered = raw.lower()
    start = lowered.find("<summary>")
    if start == -1:
        # 至少把 <analysis> 草稿段去掉
        end_analysis = lowered.find("</analysis>")
        return raw[end_analysis + len("</analysis>"):].strip() if end_analysis != -1 else raw.strip()
    start += len("<summary>")
    end = lowered.find("</summary>", start)
    return (raw[start:end] if end != -1 else raw[start:]).strip()
```

---

## 6. 主动压缩入口：`_maybe_proactive_compact`

`agent/engine/native_loop/loop.py:348-381`

```python
async def _maybe_proactive_compact(self, state: LoopState) -> None:
    """估算上下文逼近窗口上限时先摘要，避免真的撞上 413。"""
    if self._chat is None:
        return
    if state.compact_cooldown > 0:
        state.compact_cooldown -= 1
        return
    decision = compact.decide(
        state.messages, state.last_usage,
        context_window_tokens=self._config.context_window_tokens,
        buffer_tokens=self._config.compact_buffer_tokens,
        fixed_overhead_chars=self._fixed_overhead_chars,
    )
    if not decision.should:
        return
    log_kv(logger, logging.INFO, "Compact", "threshold reached",
           tokens=decision.tokens, threshold=decision.threshold,
           estimated=decision.estimated, trigger="proactive")
    with start_span("native.compact", KIND_COMPACT, trigger="proactive",
                    tokens=decision.tokens, threshold=decision.threshold,
                    estimated=decision.estimated,
                    messages_before=len(state.messages)) as span:
        compacted = await compact.compact(
            state.messages, self._chat,
            preserve_units=self._config.compact_preserve_units,
            trigger="proactive",
        )
        if compacted is None:
            span.set(ok=False).set_status("error")
            self._enter_compact_cooldown(state, "proactive")
            return
        span.set(ok=True, messages_after=len(compacted))
        self._adopt_compacted(state, compacted)
```

**执行位置**：在 `NativeLoop.run` 主循环中，**每次迭代开始**、**模型请求之前**（`loop.py:173`），紧挨着 checkpoint 提交点。

```python
# loop.py:171-180
with start_span("native.turn", KIND_TURN, iter=state.iters) as turn_span:
    # ── 主动压缩：估算逼近窗口上限就先摘要，别等真的 413 ─────────
    await self._maybe_proactive_compact(state)
    # 持久化模型 I/O 前的边界；半个 stream 失败时从此重放。
    await self._checkpoint(state, "MODEL_REQUEST")

    request_messages = self._build_request(state)
```

---

## 7. 压缩结果采用：`_adopt_compacted`

`agent/engine/native_loop/loop.py:415-424`

```python
@staticmethod
def _adopt_compacted(state: LoopState, compacted: list[Msg]) -> None:
    """采用压缩结果。

    ★ 必须同时作废 ``last_usage``：它记的是**压缩前**那次请求的 prompt_tokens，
    而 `compact.estimate_tokens` 取 usage 与字符估算的较大者。压缩已经把字符数打下去了，
    旧 usage 却会把估算值顶回原位，导致下一轮立刻触发一次冗余的二次压缩
    （多花一次摘要调用、且摘要被二次摘要，早期信息经两轮有损压缩）。
    置空后由下一次模型调用的 TurnEnd 立刻填回真值。
    """
    state.messages = compacted
    state.last_usage = None
```

**关键不变量**：压缩后必须置空 `last_usage`，否则下轮估算会被旧值顶回压缩前水平，触发冗余二次压缩，早期信息经两轮有损压缩。

---

## 8. 反应式压缩：`_reactive_compact`

`agent/engine/native_loop/loop.py:383-413`

```python
async def _reactive_compact(self, state: LoopState, *, already_emitted: bool) -> bool:
    """模型报上下文超长后的恢复：压缩历史并让调用方重来一轮。

    ``already_emitted`` 是防重复输出的闸：正文一旦已经流给了前端，
    重来一轮会让用户看到两遍内容，此时宁可如实报错。
    """
    if self._chat is None or already_emitted:
        return False
    if state.attempted_reactive_compact >= self._config.max_reactive_compacts:
        log_kv(logger, logging.WARNING, "Compact", "reactive budget exhausted",
               attempts=state.attempted_reactive_compact)
        return False
    state.attempted_reactive_compact += 1
    with start_span("native.compact", KIND_COMPACT, trigger="reactive",
                    attempt=state.attempted_reactive_compact,
                    messages_before=len(state.messages)) as span:
        compacted = await compact.compact(
            state.messages, self._chat,
            preserve_units=self._config.compact_preserve_units,
            trigger="reactive",
        )
        if compacted is None:
            span.set(ok=False).set_status("error")
            self._enter_compact_cooldown(state, "reactive")
            return False
        span.set(ok=True, messages_after=len(compacted))
        self._adopt_compacted(state, compacted)
    log_kv(logger, logging.WARNING, "LoopControl", "recovered from context overflow, retrying",
           attempt=state.attempted_reactive_compact, transition=T_REACTIVE_COMPACT)
    return True
```

**与主动压缩的差异**：

| 维度 | 主动（proactive） | 反应式（reactive） |
|---|---|---|
| 触发时机 | 每轮开始前、估算超阈值 | 模型返回 `ContextOverflowError` |
| 触发次数 | 无硬上限 | 受 `max_reactive_compacts` 限制，默认 1 |
| `already_emitted` 检查 | 不需要 | 若已流式输出给前端则放弃，避免用户看到两遍内容 |
| 返回值 | `None` | `bool`：是否成功进入重试 |

---

## 9. 冷却机制：`_enter_compact_cooldown`

`agent/engine/native_loop/loop.py:428-434`

```python
@staticmethod
def _enter_compact_cooldown(state: LoopState, trigger: str) -> None:
    state.compact_failures += 1
    state.compact_cooldown = _COMPACT_COOLDOWN_TURNS     # = 3
    log_kv(logger, logging.WARNING, "Compact", "compaction failed, cooling down",
           trigger=trigger, failures=state.compact_failures,
           cooldown_turns=_COMPACT_COOLDOWN_TURNS)
```

**语义**：压缩失败（LLM 调用异常或空摘要）后，跳过 **3 轮**再重试，而不是永久关闭——偶发失败（摘要模型抖动）不应让长会话彻底失去压缩能力。

冷却计数通过 `engine.py:220, 259` 随 `LoopState` 落 checkpoint 与从 checkpoint 恢复。

---

## 10. 时序图

### 10.1 主动压缩时序

```mermaid
sequenceDiagram
    autonumber
    participant Loop as NativeLoop.run<br/>loop.py:137
    participant PC as _maybe_proactive_compact<br/>loop.py:348
    participant Decide as compact.decide<br/>compact.py:109
    participant Est as compact.estimate_tokens<br/>compact.py:71
    participant Compact as compact.compact<br/>compact.py:194
    participant Render as render_history<br/>compact.py:143
    participant Chat as AgentChatClient.complete
    participant Adopt as _adopt_compacted<br/>loop.py:415
    participant CD as _enter_compact_cooldown<br/>loop.py:428

    Loop->>PC: 每轮迭代开始调用
    PC->>PC: 检查 self._chat / state.compact_cooldown
    alt cooldown > 0
        PC->>PC: cooldown -= 1, return
    else cooldown = 0
        PC->>Decide: decide(messages, last_usage, ...)
        Decide->>Est: estimate_tokens(...)
        Est-->>Decide: (tokens, estimated)
        Decide-->>PC: CompactDecision(should, tokens, threshold, estimated)
        alt should = False
            PC-->>Loop: return (跳过)
        else should = True
            PC->>Compact: compact(messages, chat, preserve_units, "proactive")
            Compact->>Compact: _preserved_start() 划出保留尾部
            Compact->>Render: render_history(to_summarize)
            Render-->>Compact: 结构化文本
            Compact->>Chat: complete(system=_SUMMARY_SYSTEM, user=rendered)
            Chat-->>Compact: raw 摘要文本
            Compact->>Compact: _extract_summary(raw)
            Compact-->>PC: [boundary Msg, *preserved] 或 None
            alt compacted is None
                PC->>CD: _enter_compact_cooldown(state, "proactive")
                CD-->>PC: cooldown=3, failures+=1
            else 成功
                PC->>Adopt: _adopt_compacted(state, compacted)
                Adopt->>Adopt: state.messages = compacted; state.last_usage = None
            end
        end
    end
    PC-->>Loop: 返回，继续 _checkpoint + 模型请求
```

### 10.2 反应式压缩时序

```mermaid
sequenceDiagram
    autonumber
    participant LLM as LLM Provider
    participant Loop as NativeLoop.run
    participant RC as _reactive_compact<br/>loop.py:383
    participant Compact as compact.compact
    participant Adopt as _adopt_compacted
    participant CD as _enter_compact_cooldown

    Loop->>LLM: stream(request_messages, ...)
    LLM-->>Loop: ContextOverflowError (413)
    Loop->>RC: _reactive_compact(state, already_emitted)
    alt already_emitted = True 或 budget 耗尽
        RC-->>Loop: return False (放弃)
    else 还可以重试
        RC->>Compact: compact(..., trigger="reactive")
        alt 成功
            Compact-->>RC: [boundary, *preserved]
            RC->>Adopt: _adopt_compacted(state, compacted)
            Adopt-->>RC: messages 替换、last_usage 置空
            RC-->>Loop: return True
            Loop->>Loop: continue（transition=T_REACTIVE_COMPACT）
        else 失败
            Compact-->>RC: None
            RC->>CD: _enter_compact_cooldown(state, "reactive")
            RC-->>Loop: return False
        end
    end
```

---

## 11. 调用栈图（含源码定位）

### 11.1 主动压缩调用栈

```text
NativeLoop.run()                                              loop.py:137
 └─ await self._maybe_proactive_compact(state)                loop.py:348   ★ 入口
     ├─ if self._chat is None → return
     ├─ if state.compact_cooldown > 0 → cooldown -= 1; return
     ├─ compact.decide(                                        compact.py:109  ★ 触发判定
     │     └─ estimate_tokens(messages, last_usage, ...)       compact.py:71
     │         ├─ _char_tokens(messages, fixed_overhead_chars) compact.py:100
     │         │    └─ 遍历 messages，累加 content + tool_call 名字/参数长度
     │         │    └─ 总字符 / CHARS_PER_TOKEN(1.5)
     │         └─ 若有 last_usage.prompt_tokens：
     │              return max(prompt_tokens, char_estimate), False
     │              否则 return char_estimate, True
     │
     │  threshold = context_window_tokens - buffer_tokens
     │  should = tokens >= threshold
     │
     ├─ if not decision.should → return
     │
     └─ compact.compact(                                       compact.py:194  ★ 执行压缩
           messages, self._chat,
           preserve_units=self._config.compact_preserve_units,
           trigger="proactive")
         ├─ _preserved_start(messages, preserve_units)         compact.py:182
         │    └─ atomic_units(messages)                        messages.py:154
         │         └─ 切分：assistant+tool_calls 与紧随的 tool 为一个 Unit
         │    └─ return units[len - preserve_units].start
         │
         ├─ to_summarize = messages[:start]
         ├─ preserved = messages[start:]
         │
         ├─ render_history(to_summarize)                       compact.py:143
         │    ├─ 跳过 system
         │    ├─ image_url → "[图片]"
         │    ├─ 单条 > 2000 字符截断
         │    ├─ 整段 > 60000 字符只保留尾部 60k（越早越先丢）
         │    └─ 拼装 [用户]/[助手]/[发起工具调用]/[工具结果 X 成功/失败]
         │
         ├─ await chat.complete(                               compact.py:216
         │      system=_SUMMARY_SYSTEM,                        compact.py:45
         │      user=f"请压缩以下对话历史：\n\n{rendered}",
         │      max_tokens=4096,  temperature=0.1)
         │
         ├─ _extract_summary(raw)                              compact.py:169
         │    └─ 取 <summary> 段；无则剥 <analysis>；再无则全文
         │
         └─ return [ Msg(role=user,
                        content=_SUMMARY_PREFIX + summary,
                        kind=KIND_COMPACT_SUMMARY),
                     *preserved ]

    回到 _maybe_proactive_compact：
     ├─ 若 compacted is None
     │    └─ self._enter_compact_cooldown(state, "proactive")  loop.py:428
     │        └─ state.compact_failures += 1
     │        └─ state.compact_cooldown = 3
     │
     └─ 否则
          └─ self._adopt_compacted(state, compacted)           loop.py:415
              ├─ state.messages = compacted
              └─ state.last_usage = None  ★ 关键：防止旧 usage 顶回估算值
```

### 11.2 反应式压缩调用栈

```text
NativeLoop.run()                                              loop.py:137
 └─ except ContextOverflowError:
     └─ if await self._reactive_compact(state, already_emitted):   loop.py:383
            continue (transition=T_REACTIVE_COMPACT)
         ├─ if self._chat is None or already_emitted → False
         ├─ if state.attempted_reactive_compact >= max_reactive_compacts → False
         ├─ state.attempted_reactive_compact += 1
         └─ compact.compact(..., trigger="reactive")               compact.py:194
             └─ （同主动压缩的执行子流程）
                 ├─ 成功 → _adopt_compacted(state, compacted); return True
                 └─ 失败 → _enter_compact_cooldown(state, "reactive"); return False
```

### 11.3 调用点总览

```text
agent/engine/native_loop/engine.py:144-146
 └─ LoopConfig(
        context_window_tokens=settings.context_window_tokens,
        compact_buffer_tokens=settings.compact_buffer_tokens,
        compact_preserve_units=settings.compact_preserve_units,
        ...
    )

agent/engine/native_loop/engine.py:220
 └─ checkpoint 序列化时记录 state.compact_cooldown

agent/engine/native_loop/engine.py:259
 └─ checkpoint 反序列化时恢复 compact_cooldown

agent/engine/native_loop/loop.py:173
 └─ await self._maybe_proactive_compact(state)   ★ 主循环中的主动入口

agent/engine/native_loop/loop.py:175
 └─ await self._checkpoint(state, "MODEL_REQUEST")   ★ 压缩后紧跟着 checkpoint
```

---

## 12. 关键不变量与诚实边界

### 12.1 不变量

1. **原子单元边界**：任何裁剪/压缩都必须在 `Unit` 边界进行。`_preserved_start` 通过 `atomic_units` 保证不会把 assistant+tool_call 与其 tool 响应劈开，否则 OpenAI 协议直接 400。
2. **保留尾部不动**：压缩只替换早期历史，最近的 `preserve_units` 个原子单元原样保留。
3. **压缩后置空 `last_usage`**：避免旧 usage 把下轮估算顶回压缩前水平，导致冗余二次压缩。
4. **摘要有渲染上限**：`_PER_MESSAGE_RENDER_MAX_CHARS=2000`、`_HISTORY_RENDER_MAX_CHARS=60000`，避免"为压缩而撑爆摘要请求"。
5. **冷却 + 反应预算**：失败后 `_COMPACT_COOLDOWN_TURNS=3` 轮再重试；反应式压缩受 `max_reactive_compacts`（默认 1）限制，防止"压缩 → 仍超长 → 再压缩"的死循环。
6. **摘要输出预算充足**：`_SUMMARY_MAX_TOKENS=4096`，覆盖 `AgentChatClient` 默认 512，避免 7 段式摘要被截断。
7. **Boundary 即摘要**：`KIND_COMPACT_SUMMARY` 的 Msg 同时充当 compact boundary，`messages_after_boundary` 在正常链路上返回全量，只为让"边界"语义显式化（`messages.py:215`）。

### 12.2 诚实边界

| 风险点 | 现状 | 备注 |
|---|---|---|
| Token 估算精度 | 上游不返回 usage 时为字符估算，`estimated=true` 如实打标 | 13k buffer 用于吸收偏差 |
| 摘要有损 | 早期原文被整体替换，**不可回溯** | 这是 CC 的主动取舍：内存与上下文真正压下去 |
| 二次摘要 | 摘要本身也会成为后续压缩的输入 | 通过冷却、置空 usage 与 preserve_units 缓解 |
| 单轮超限 | `to_summarize` 为空时 `compact()` 返回 None | 压缩帮不上忙，交给调用方熔断 |
| 流式输出后 413 | `already_emitted=True` 时放弃反应式压缩 | 避免用户看到两遍内容 |
| 工具体积治理 | `apply_tool_result_budget` 在请求副本上做，不写回历史 | 与"整段丢弃"分工明确 |

### 12.3 与 CC 的差异（本仓库最小完整版）

- ❌ 不做 microcompact（按 tool_use_id 的服务端缓存编辑）
- ❌ 不做 snip
- ❌ 不做 context-collapse
- ❌ 不做 post-compact 文件恢复
- ✅ 保留阈值触发、结构化摘要、compact boundary、413 反应式恢复

---

## 参考阅读

- `agent/engine/native_loop/loop.py` —— 主循环 + 主动/反应式入口 + 冷却 + 采用
- `agent/engine/native_loop/compact.py` —— 压缩算法本体
- `agent/engine/native_loop/messages.py` —— 消息模型 + 原子单元 + boundary
- `agent/engine/native_loop/engine.py` —— LoopConfig 注入 + checkpoint 序列化
