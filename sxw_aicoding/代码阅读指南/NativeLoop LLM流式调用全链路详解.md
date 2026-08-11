# NativeLoop LLM 流式调用全链路详解

本文档深入分析 `native_loop` 的底层物理层——直面 OpenAI 兼容流式协议的完整流程：从组装 OpenAI 请求报文、发起 SSE 流式调用、逐 chunk 解析累积 tool_calls、到向上层抛 `TextDelta` / `ToolCallReady` / `TurnEnd`。

附带**真实的 chunk 形状示例**（来自 `scripts/probe_dashscope_tool_stream.py` 探针）与**完整时序图 / 调用栈图**。

---

## 目录

- [1. 核心问题与总体思路](#1-核心问题与总体思路)
- [2. 关键源码位置索引](#2-关键源码位置索引)
- [3. 各层数据结构概览](#3-各层数据结构概览)
- [4. OpenAI 流式报文的真实形状](#4-openai-流式报文的真实形状)
- [5. 上层组装：`_build_request` + `wire_declarations`](#5-上层组装_build_request--wire_declarations)
- [6. 物理层：`NativeLlmClient.stream`](#6-物理层_nativellmclientstream)
- [7. 累积器：`_ToolCallAccumulator`](#7-累积器_toolcallaccumulator)
- [8. 就绪判定与提前派发](#8-就绪判定与提前派发)
- [9. 异常分类与超长恢复](#9-异常分类与超长恢复)
- [10. 端到端时序图](#10-端到端时序图)
- [11. 完整调用栈图（含源码定位）](#11-完整调用栈图含源码定位)
- [12. 关键不变量](#12-关键不变量)

---

## 1. 核心问题与总体思路

### 1.1 问题场景

`native_loop` 的自研循环要直面 OpenAI 兼容协议的**流式 SSE**，需要解决：

1. **各家分片规则不一致**：标准 OpenAI 首片带 `id + name`、后续片只追加 `arguments` 字符串；部分厂商一次性吐完整 tool_call。累积器必须两种都吃。
2. **流式工具执行**：为降低端到端延迟，希望**一个 tool call 参数凑齐就立刻派发执行**，不等整轮流完。但 JSON 参数可能半截，必须有"已就绪"的安全闸。
3. **上下文超长恢复**：模型报 413 不是终止条件，而是"压缩后重来一轮"。
4. **token 用量回压**：流尾的 `usage` 用于压缩阈值估算，但 provider 不一定支持 `include_usage`，要优雅降级。

### 1.2 总体架构

```text
                     ┌───────────────────────────────┐
                     │ NativeLoop.run() 主循环        │
                     │ loop.py:137                    │
                     └──────────────┬────────────────┘
                                    │ await self._client.stream(...)
                                    ▼
                     ┌───────────────────────────────┐
                     │ NativeLlmClient.stream()      │
                     │ llm_client.py:191              │
                     │   - 组装 payload               │
                     │   - 打开 llm span              │
                     │   - 调用 _consume 迭代          │
                     │   - 异常分类 + 降级重试          │
                     └──────────────┬────────────────┘
                                    │ async for chunk in stream
                                    ▼
                     ┌───────────────────────────────┐
                     │ NativeLlmClient._consume()    │
                     │ llm_client.py:274              │
                     │   - 累积 tool_calls            │
                     │   - 产出 TextDelta             │
                     │   - 就绪时产出 ToolCallReady    │
                     │   - 流结束产出 TurnEnd         │
                     └──────────────┬────────────────┘
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        │                           │                           │
        ▼                           ▼                           ▼
  ┌──────────┐              ┌────────────────┐          ┌──────────┐
  │TextDelta │              │ToolCallReady   │          │TurnEnd   │
  │(文本增量)│              │(完整 tool call)│          │(轮结束)  │
  └──────────┘              └────────────────┘          └──────────┘
        │                           │                           │
        │   openai.AsyncOpenAI.chat.completions.create(stream=True)
        │                           │
        ▼                           ▼                           ▼
     ┌─────────────────────────────────────────────────────────────┐
     │  DashScope / OpenAI / 任意 OpenAI 兼容 Provider (SSE 流)   │
     └─────────────────────────────────────────────────────────────┘
```

---

## 2. 关键源码位置索引

| 模块 | 文件路径 | 关键符号 |
|---|---|---|
| 流式客户端 | `agent/engine/native_loop/llm_client.py` | `NativeLlmClient`, `stream`, `_consume`, `_ToolCallAccumulator`, `_PartialCall`, `TextDelta`, `ToolCallReady`, `TurnEnd`, `StreamItem`, `ContextOverflowError`, `_classify`, `_observe` |
| 主循环 | `agent/engine/native_loop/loop.py` | `NativeLoop.run`, `_build_request`, `_execute`, `_call_events`, `_result_events` |
| 消息模型 | `agent/engine/native_loop/messages.py` | `Msg`, `ToolCall`, `Usage`, `to_wire`, `CHARS_PER_TOKEN` |
| 工具执行 | `agent/engine/native_loop/executor.py` | `ToolOutcome`, `parse_arguments`, `run_calls`, `execute_one` |
| 工具注册 | `agent/engine/native_loop/tools.py` | `ToolRegistry`, `ToolSpec`, `wire_declarations` |
| 流式探针 | `scripts/probe_dashscope_tool_stream.py` | 真实 chunk 形状实测（换 provider 必须重跑） |
| 异常分类 | `agent/llm/exceptions.py` | `classify_llm_error`, `CONTEXT_OVERFLOW` |

---

## 3. 各层数据结构概览

### 3.1 给 LLM 的请求（上层组装，`Msg.to_wire` 转换后）

`messages.py:49-70`：

```python
def to_wire(self) -> dict[str, Any]:
    if self.role == "tool":
        return {"role": "tool", "tool_call_id": self.tool_call_id or "",
                "content": _as_text(self.content)}
    wire: dict[str, Any] = {"role": self.role}
    wire["content"] = self.content if self.content not in ("", None) else None
    if self.tool_calls:
        wire["tool_calls"] = [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.name, "arguments": tc.arguments or "{}"}}
            for tc in self.tool_calls
        ]
    return wire
```

**示例（最终进入 OpenAI 请求体的 messages）**：

```json
[
  {"role": "system", "content": "你是一个有用的助手..."},
  {"role": "user", "content": "同时算 12*12 并查杭州天气"},
  {"role": "assistant",
   "tool_calls": [
     {"id": "call_abc", "type": "function",
      "function": {"name": "calculator", "arguments": "{\"expression\":\"12*12\"}"}},
     {"id": "call_def", "type": "function",
      "function": {"name": "get_weather", "arguments": "{\"city\":\"杭州\"}"}}
   ]},
  {"role": "tool", "tool_call_id": "call_abc", "content": "144"},
  {"role": "tool", "tool_call_id": "call_def", "content": "晴，25℃"}
]
```

**示例（tools 参数，由 `ToolRegistry.wire_declarations()` 产出）**：

```json
[
  {"type": "function", "function": {
      "name": "calculator",
      "description": "计算一个数学算术表达式并返回结果。",
      "parameters": {"type": "object",
                     "properties": {"expression": {"type": "string", "description": "..."}},
                     "required": ["expression"]}}},
  {"type": "function", "function": {
      "name": "get_weather",
      "description": "查询指定城市的天气。",
      "parameters": {"type": "object",
                     "properties": {"city": {"type": "string", "description": "..."}},
                     "required": ["city"]}}}
]
```

### 3.2 OpenAI SSE 流的 chunk 形状

来自探针 `scripts/probe_dashscope_tool_stream.py` 的真实测量结果——**标准分片形态**：

```text
[并行工具调用 case：parallel_tools]

chunk #001  tool_call idx=0  id='call_abc123…'  name='calculator'  arguments=''
chunk #002  tool_call idx=0  id=None             name=None          arguments='{"exp'
chunk #003  tool_call idx=0  id=None             name=None          arguments='ression":"1'
chunk #004  tool_call idx=0  id=None             name=None          arguments='2*12"}'
chunk #005  tool_call idx=1  id='call_def456…'  name='get_weather' arguments=''
chunk #006  tool_call idx=1  id=None             name=None          arguments='{"cit'
chunk #007  tool_call idx=1  id=None             name=None          arguments='y":"杭州"}'
chunk #008  <no choices>  (finish_reason=None)
chunk #009  finish_reason='tool_calls'  usage=Usage(prompt_tokens=..., completion_tokens=..., total_tokens=...)
```

**关键事实（所有 OpenAI 兼容 provider 都遵循）**：
- `id` / `name` **只在首片出现**，后续片为空
- `arguments` 是**字符串分片**，必须逐片拼接
- `index` 区分同一轮内的多个 tool call
- `usage` 通常单独一个 chunk（无 choices），且仅在启用 `include_usage` 时返回
- 部分厂商可能一次性吐完整 tool_call（单分片），累积器必须两种都吃

### 3.3 上层产出的三种 StreamItem

`llm_client.py:62-83`：

```python
@dataclass
class TextDelta:
    text: str                                # 正文增量

@dataclass
class ToolCallReady:
    call: ToolCall                           # 参数已完整、可投递的完整 tool call

@dataclass
class TurnEnd:
    finish_reason: Optional[str] = None      # 通常为 "tool_calls" 或 "stop"
    usage: Optional[Usage] = None            # token 用量

StreamItem = TextDelta | ToolCallReady | TurnEnd
```

### 3.4 ToolCall：LLM 的调用决策（非执行结果）

`messages.py:24-36`：

```python
@dataclass
class ToolCall:
    id: str                  # provider 给的 call id（native 后续覆盖为 logical_key）
    name: str                # 工具名，例如 "knowledge_search"
    arguments: str = ""      # ★ 原始 JSON 字符串（不解析），保留失败可喂回模型
    logical_key: str = ""    # native_loop 自造的稳定身份（用于 replay / Broker 匹配）
```

---

## 4. OpenAI 流式报文的真实形状

### 4.1 请求体（payload）

`llm_client.py:200-212`：

```python
payload: dict[str, Any] = {
    "model": self._model,                      # e.g. "qwen-max"
    "messages": to_wire(messages),             # ★ 见上文 3.1 的 messages 示例
    "stream": True,                            # ★ 必须流式
    "temperature": 0.2,
    "extra_body": {"enable_thinking": False},  # 关掉 Qwen 思考过程，避免混进正文流
}
if tools:
    payload["tools"] = tools                   # ★ 见上文 3.1 的 tools 示例
if self._include_usage:
    payload["stream_options"] = {"include_usage": True}
```

### 4.2 流中 chunk 的 Python 对象结构

`openai` SDK 把 SSE 流解析成如下对象（`_consume` 用 `getattr` 安全读取）：

```python
chunk = ChatCompletionChunk(
    id="chatcmpl-xxx",
    choices=[
        Choice(
            delta=ChoiceDelta(
                content="我来查一下…" | None,          # 文本增量（可空）
                tool_calls=[                           # 工具分片数组（可空）
                    ChatCompletionChunkToolCall(
                        index=0,                       # ★ 关键：区分第几个 tool call
                        id="call_abc123" | None,       # 首片有，后续片 None
                        type="function",
                        function=Function(
                            name="calculator" | None,      # 首片有，后续片 None
                            arguments='{"exp'              # 字符串分片，需拼接
                        )
                    )
                ]
            ),
            finish_reason="tool_calls" | None | "stop"
        )
    ],
    usage=Usage(prompt_tokens=..., completion_tokens=..., total_tokens=...) | None  # 仅末片
)
```

### 4.3 探针输出的真实示例

单工具：

```text
[case] single_tool  tools=True
  #001 tool_call idx=0 type='function' id='call_abc1…' name='calculator' arguments=''
  #002 tool_call idx=0 type=None id=None name=None arguments='{"expression":'
  #003 tool_call idx=0 type=None id=None name=None arguments='"3*(4+5)"'
  #004 tool_call idx=0 type=None id=None name=None arguments='}'
  #005 finish_reason='tool_calls'
  ---- 结论 ----
  idx=0 fragments=4 id_at=[1] name_at=[1] arg_fragments=3
    joined_arguments='{"expression":"3*(4+5)"}'  → json_ok type=dict
  → 线形状：标准分片（arguments 需跨 chunk 拼接）
```

并行工具：

```text
[case] parallel_tools  tools=True
  #001 tool_call idx=0 id='call_a1…' name='calculator' arguments=''
  #002 tool_call idx=0 id=None name=None arguments='{"expression":"12*'
  #003 tool_call idx=0 id=None name=None arguments='12"}'
  #004 tool_call idx=1 id='call_d1…' name='get_weather' arguments=''
  #005 tool_call idx=1 id=None name=None arguments='{"city":"杭州"}'
  #006 finish_reason='tool_calls'
  ---- 结论 ----
  idx=0 fragments=3 id_at=[1] name_at=[1]
  idx=1 fragments=2 id_at=[1] name_at=[1]
  → 线形状：标准分片（arguments 需跨 chunk 拼接）
```

---

## 5. 上层组装：`_build_request` + `wire_declarations`

`loop.py:438-476`（`_build_request` 定义）+ `tools.py:96-97`：

```python
# 主循环的每轮迭代
with start_span("native.turn", KIND_TURN, iter=state.iters) as turn_span:
    await self._maybe_proactive_compact(state)           # ① 主动压缩
    await self._checkpoint(state, "MODEL_REQUEST")       # ② checkpoint
    request_messages = self._build_request(state)        # ③ 组装消息视图
    async for item in self._client.stream(
        messages=request_messages,
        tools=self._registry.wire_declarations() or None,  # ④ 工具面
        allow_early_tool_dispatch=cfg.streaming_tool_exec, # ⑤ 是否提前派发
    ):
        ...
```

`_build_request` 给 LLM 看的"视野"：

```text
request = [
    system: self._system,                           # 系统指令
    *clone(messages_after_boundary(state.messages)),# 历史（compact boundary 后）
        └─ apply_tool_result_budget(max_chars)      #    超长 tool_result 截断
    Msg(user, PLAN_CONTINUATION_REMINDER),           # 计划续推提醒（按需）
    Msg(user, FORCE_SUMMARY_REMINDER),               # 软收尾劝停（按需）
]
```

**关键点**：
- 返回**副本**（`clone`），体积治理与临时提醒只作用于本次请求，不写回历史
- `wire_declarations()` 把所有注册工具转 OpenAI `tools` JSON Schema 列表
- 空列表 `[] or None` 降级为 None，避免空列表在某些 provider 上校验失败

---

## 6. 物理层：`NativeLlmClient.stream`

`llm_client.py:191-272`：

```python
async def stream(
    self, *, messages, tools=None, allow_early_tool_dispatch=True, temperature=0.2,
) -> AsyncIterator[StreamItem]:
    payload = {...}                          # 见 4.1
    request_chars = _payload_chars(payload)  # 体积兜底判据用

    span = open_span("native.llm", KIND_LLM, model=self._model, ...)
    span.set_payload("messages", [m.to_wire() for m in messages])
    status = STATUS_OK
    try:
        emitted = False
        try:
            async for item in self._consume(payload, allow_early_tool_dispatch):
                emitted = True
                yield _observe(span, item)          # ★ 给 span 记一笔，原样放行
            return
        except openai.BadRequestError as exc:
            # 仅"上游不认 stream_options"一种情况值得降级重试
            if emitted or not (self._include_usage and _mentions_stream_options(exc)):
                raise self._classify(exc, request_chars) from exc
            self._include_usage = False              # ★ 永久记住 provider 不支持
            payload.pop("stream_options", None)
            span.set(stream_options_downgraded=True)
        except Exception as exc:
            raise self._classify(exc, request_chars) from exc

        # 降级重试（不带 usage）
        try:
            async for item in self._consume(payload, allow_early_tool_dispatch):
                yield _observe(span, item)
        except Exception as exc:
            raise self._classify(exc, request_chars) from exc
    except GeneratorExit:
        status = STATUS_CANCELLED
        raise
    except BaseException as exc:
        status = STATUS_CANCELLED if isinstance(exc, CancelledError) else STATUS_ERROR
        ...
        raise
    finally:
        close_span(span, status)
```

**关键语义**：
- `open_span` 而非 `with start_span`：后者会把 llm span 压进 contextvar，而**流式工具执行**是在本流迭代过程中 `asyncio.create_task` 投递的——工具 span 应该挂到 turn 下，不是 llm 下
- `stream_options` 降级只发生一次：`self._include_usage = False` 后整进程记住，避免每轮都试错
- `emitted` 守卫：一旦吐过内容绝不重试，避免重复输出

---

## 7. 累积器：`_ToolCallAccumulator`

`llm_client.py:88-164`：

```python
@dataclass
class _PartialCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""      # ★ 逐片拼接的原始 JSON 字符串
    emitted: bool = False

    def merge(self, call_id, name, arguments):
        if call_id and not self.id: self.id = str(call_id)       # ★ id 取首次非空
        if name and not self.name: self.name = str(name)         # ★ name 取首次非空
        if arguments: self.arguments += str(arguments)            # ★ arguments 持续拼接

    def is_parseable(self) -> bool:
        """参数能解析成 JSON 对象 —— 提前判定就绪的安全闸。"""
        text = self.arguments.strip()
        if not text: return False
        try:
            return isinstance(json.loads(text), dict)             # ★ 必须是 dict
        except (TypeError, ValueError):
            return False

    def to_call(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=self.arguments)


class _ToolCallAccumulator:
    def __init__(self):
        self._calls: dict[int, _PartialCall] = {}     # ★ 按 index 分桶
        self._max_index: int = -1

    def add(self, index, call_id, name, arguments):
        partial = self._calls.get(index) or _PartialCall(index=index)
        self._calls[index] = partial
        if partial.emitted and arguments:
            log_kv(logger, WARNING, "fragment arrived after emitted", index=index)
        partial.merge(call_id, name, arguments)
        self._max_index = max(self._max_index, index)

    def take_ready(self, *, allow_early):              # ★ 流式派发
        if not allow_early: return []
        ready = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted: continue
            # ★ 三个条件：已出现更高 index + name 非空 + arguments 可 parse
            if index < self._max_index and partial.name and partial.is_parseable():
                partial.emitted = True
                ready.append(partial.to_call())
        return ready

    def take_remaining(self):                          # ★ 流结束兜底
        remaining = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted: continue
            partial.emitted = True
            remaining.append(partial.to_call())
        return remaining
```

---

## 8. 就绪判定与提前派发

### 8.1 就绪的三条件

```text
partial.emitted == False
  AND index < self._max_index         (1) 已出现更高 index
  AND partial.name                    (2) 函数名已拿到
  AND partial.is_parseable()          (3) arguments 能 parse 成 dict
```

**为什么需要"已出现更高 index"**：标准分片下，后续片的 arguments 会持续追加；只有看到下一个 index 的起始分片，才能确认当前 index 的 arguments 已经完整。这是"提前派发"的安全闸——半截 JSON 不能 parse 成 dict，`is_parseable()` 返回 False，不会误判就绪。

### 8.2 上层如何使用 `ToolCallReady`

`loop.py:200-221`：

```python
elif isinstance(item, ToolCallReady):
    item.call.logical_key = (
        f"native:turn:{state.iters - 1}:call:{len(ready_calls)}"
    )                                                   # ★ 覆盖为稳定身份
    ready_calls.append(item.call)                       # ★ 累积本轮
    for ev in self._call_events(item.call):
        yield ev                                        # ★ 翻译为 StreamEvent
    if early_allowed and self._is_concurrency_safe(item.call):
        turn_span.incr("early_dispatched")
        early_tasks.append((item.call, asyncio.create_task(
            self._execute(item.call, state),            # ★ 立刻派发执行
        )))
    else:
        early_allowed = False                            # 一旦出现非安全工具，后续全推迟
        deferred_calls.append(item.call)
```

**CC 的 `partitionToolCalls` 语义**：并发安全的前缀立刻派发，遇到第一个非安全工具后所有后续一律推迟到流结束后按批次串行执行。

---

## 9. 异常分类与超长恢复

### 9.1 异常分类：`_classify`

`llm_client.py:321-350`：

```python
def _classify(self, exc, request_chars=0):
    kind = classify_llm_error(exc)                     # ★ 共享判据
    message = f"{type(exc).__name__}: {exc}"
    if kind == CONTEXT_OVERFLOW:
        return ContextOverflowError(message)

    # ★ provider 无关的体积兜底判据
    if (isinstance(exc, openai.BadRequestError)
            and request_chars >= self._overflow_chars_threshold > 0):
        log_kv(logger, WARNING, "unrecognized 400 with oversized request, "
                                "treating as context overflow", ...)
        return ContextOverflowError(message)

    return NativeLlmError(message, kind)
```

**兜底判据**：400 + 请求体积 ≥ `context_window_tokens × 1.5 × 0.9` 字符，按超长处理。
- 误判代价 = 多做一次压缩后重试（有单次守卫），远小于漏判导致恢复链路完全失效

### 9.2 上层如何恢复

`loop.py:226-242`：

```python
except ContextOverflowError as exc:
    # ★ 恢复优先于失败
    await self._cancel_tasks(early_tasks)
    recovered = await self._reactive_compact(
        state, already_emitted=bool(text_parts))
    if recovered:
        state.iters -= 1        # 这一轮没真正跑成，不计入软收尾预算
        state.transition = T_REACTIVE_COMPACT
        turn_span.set(transition=T_REACTIVE_COMPACT, recovered=True)
        continue                 # ★ 重来一轮
    ...
```

---

## 10. 端到端时序图

### 10.1 正常并行工具调用

```mermaid
sequenceDiagram
    autonumber
    participant Loop as NativeLoop.run<br/>loop.py:137
    participant PC as _maybe_proactive_compact<br/>loop.py:348
    participant Client as NativeLlmClient.stream<br/>llm_client.py:191
    participant Consume as _consume<br/>llm_client.py:274
    participant Acc as _ToolCallAccumulator<br/>llm_client.py:120
    participant Provider as DashScope/OpenAI<br/>(SSE 流)

    Loop->>PC: ① 主动压缩
    PC-->>Loop: done
    Loop->>Client: ② await stream(messages, tools, allow_early=True)
    Client->>Client: ③ 组装 payload（to_wire(messages) + wire_declarations）
    Client->>Client: ④ open_span("native.llm")
    Client->>Provider: ⑤ chat.completions.create(stream=True)
    Provider-->>Client: SSE chunk stream

    loop 每个 chunk
        Client->>Consume: async for chunk in stream
        Consume->>Consume: ⑥ 解析 choice.delta
        alt delta.content 非空
            Consume-->>Client: yield TextDelta(content)
            Client-->>Loop: yield TextDelta
            Loop-->>Loop: yield StreamEvent("text", {delta})
        else delta.tool_calls 非空
            Consume->>Acc: add(index, id, name, arguments)
            Consume->>Acc: take_ready(allow_early=True)
            alt 三个就绪条件都满足
                Acc-->>Consume: [ToolCall]
                Consume-->>Client: yield ToolCallReady(call)
                Client->>Client: ⑦ _observe(span, item)
                Client-->>Loop: yield ToolCallReady
                Loop->>Loop: ⑧ 赋值 logical_key、ready_calls.append
                Loop->>Loop: ⑨ yield StreamEvent("tool_call")
                Loop->>Loop: ⑩ asyncio.create_task(_execute)（若并发安全）
            else 未就绪
                Acc-->>Consume: []
            end
        else chunk.usage 非空
            Consume->>Consume: ⑪ 记录 usage
        end
    end

    Consume->>Acc: take_remaining()
    Acc-->>Consume: [剩余的 ToolCall]
    Consume-->>Client: yield ToolCallReady（每个剩余的）
    Consume-->>Client: yield TurnEnd(finish_reason, usage)
    Client-->>Loop: yield TurnEnd
    Loop->>Loop: ⑫ 记录 state.last_usage
    Client->>Client: ⑬ close_span(span, STATUS_OK)
```

### 10.2 上下文超长恢复

```mermaid
sequenceDiagram
    autonumber
    participant Loop as NativeLoop.run
    participant Client as NativeLlmClient.stream
    participant Consume as _consume
    participant Classify as _classify
    participant Provider as DashScope/OpenAI
    participant RC as _reactive_compact

    Loop->>Client: await stream(...)
    Client->>Provider: chat.completions.create(stream=True)
    Provider-->>Client: 400 BadRequestError (context_length_exceeded)
    Client->>Classify: _classify(exc, request_chars)
    alt 关键词命中 OR 体积兜底命中
        Classify-->>Client: ContextOverflowError
    else
        Classify-->>Client: NativeLlmError(other)
        Client-->>Loop: raise NativeLlmError → Loop 报错退出
    end
    Client-->>Loop: raise ContextOverflowError
    Loop->>Loop: except ContextOverflowError
    Loop->>RC: _reactive_compact(state, already_emitted=False)
    alt 成功
        RC-->>Loop: True
        Loop->>Loop: state.iters -= 1; continue
        Note over Loop: 重来一轮（_maybe_proactive_compact 已压过，不会再压）
    else budget 耗尽 OR 已流式输出给前端
        RC-->>Loop: False
        Loop->>Loop: 报错退出
    end
```

---

## 11. 完整调用栈图（含源码定位）

```text
agent/engine/native_loop/engine.py
 └─ NativeLoopEngineAdapter.execute()
     └─ NativeLoop(client=..., registry=..., system_instruction=..., config=...).run()

agent/engine/native_loop/loop.py:137
 └─ NativeLoop.run()                                            ★ 主循环
     │
     │  每轮迭代：
     │
     ├─ await self._maybe_proactive_compact(state)              loop.py:173（调用处）/ 348（定义）
     │     └─ （详见 NativeLoop上下文压缩全链路详解.md）
     │
     ├─ await self._checkpoint(state, "MODEL_REQUEST")          loop.py:175
     │
     ├─ request_messages = self._build_request(state)           loop.py:180（调用处）/ 438（定义）
     │     ├─ clone(messages_after_boundary(state.messages))    messages.py:215
     │     ├─ apply_tool_result_budget(live, max_chars)         messages.py:231
     │     ├─ [Msg(system, self._system), *live]
     │     └─ 按需追加 PLAN_CONTINUATION / FORCE_SUMMARY 提醒
     │
     └─ async for item in self._client.stream(                  loop.py:191
              messages=request_messages,
              tools=self._registry.wire_declarations() or None,  tools.py:96
              allow_early_tool_dispatch=cfg.streaming_tool_exec,
          ):
          │
          │  ★ 进入 NativeLlmClient.stream
          │
          └─ NativeLlmClient.stream()                            llm_client.py:191
              ├─ payload = {                                       llm_client.py:200
              │     "model": self._model,
              │     "messages": to_wire(messages),
              │     "stream": True,
              │     "temperature": 0.2,
              │     "extra_body": {"enable_thinking": False},
              │     "tools": [...],                              # 如有
              │     "stream_options": {"include_usage": True},   # 如支持
              │   }
              │
              ├─ request_chars = _payload_chars(payload)           llm_client.py:216
              ├─ span = open_span("native.llm", KIND_LLM, ...)    llm_client.py:228
              │
              └─ try:                                              llm_client.py:237
                   │
                   └─ async for item in self._consume(payload, allow_early):
                        │
                        └─ _consume(payload, allow_early)            llm_client.py:274
                             ├─ accumulator = _ToolCallAccumulator()
                             │
                             ├─ async with await self._client.chat.completions.create(**payload) as stream:
                             │     │
                             │     │  ★ 物理层：OpenAI SDK 发起 HTTP 请求，拿 SSE 流
                             │     │
                             │     └─ async for chunk in stream:        llm_client.py:284
                             │           │
                             │           │  chunk 的真实形状（实测）：
                             │           │  chunk.choices[0].delta =
                             │           │    ChoiceDelta(
                             │           │      content="我来查一下" | None,
                             │           │      tool_calls=[
                             │           │        ChatCompletionChunkToolCall(
                             │           │          index=0,
                             │           │          id="call_abc" | None,
                             │           │          type="function",
                             │           │          function=Function(
                             │           │            name="calculator" | None,
                             │           │            arguments='{"exp'     ← 字符串分片
                             │           │          )
                             │           │        )
                             │           │      ]
                             │           │    )
                             │           │
                             │           ├─ chunk.usage 非空 → 记录 usage      llm_client.py:285
                             │           ├─ delta.content 非空：
                             │           │     yield TextDelta(content)        llm_client.py:303
                             │           └─ delta.tool_calls 非空：
                             │                 for raw in delta.tool_calls:
                             │                     accumulator.add(            llm_client.py:308
                             │                         0 if raw.index is None else int(raw.index),
                             │                         getattr(raw, "id", None),
                             │                         raw.function.name,
                             │                         raw.function.arguments,
                             │                     )
                             │                 for call in accumulator.take_ready(allow_early=...):
                             │                     yield ToolCallReady(call)    llm_client.py:314
                             │
                             ├─ for call in accumulator.take_remaining():       llm_client.py:317
                             │       yield ToolCallReady(call)
                             └─ yield TurnEnd(finish_reason, usage)             llm_client.py:319
                             │
                             │  ★ 回到 NativeLlmClient.stream
                             │
                             └─ yield _observe(span, item)                    llm_client.py:240
                                   │
                                   │  _observe 给 span 记一笔，原样放行
                                   │  TextDelta → span.incr("text_chars", len)
                                   │  ToolCallReady → span.append("tool_calls", name)
                                   │  TurnEnd → span.set(finish_reason, tokens...)
                                   │
                                   └─ → yield 给上层 NativeLoop.run
          
          │  ★ 回到 NativeLoop.run 的 async for item 分支
          │
          ├─ if isinstance(item, TextDelta):                    loop.py:197
          │     text_parts.append(item.text)
          │     yield StreamEvent("text", {"delta": item.text})
          │
          ├─ elif isinstance(item, ToolCallReady):              loop.py:200
          │     item.call.logical_key = "native:turn:N:call:M"  loop.py:205
          │     ready_calls.append(item.call)
          │     for ev in self._call_events(item.call):         loop.py:548
          │         yield ev
          │     if early_allowed and self._is_concurrency_safe(item.call):
          │         early_tasks.append((item.call, asyncio.create_task(
          │             self._execute(item.call, state),        loop.py:217（调用处）/ 520（定义）
          │         )))
          │     else:
          │         early_allowed = False
          │         deferred_calls.append(item.call)
          │
          └─ elif isinstance(item, TurnEnd):                    loop.py:222
                finish_reason = item.finish_reason
                state.last_usage = item.usage                   # ★ 给下轮压缩阈值用

     │
     │  ★ 流结束后，工具执行与续推
     │
     ├─ await 所有 early_tasks
     ├─ 串行执行 deferred_calls（如 cfg.streaming_tool_exec 关掉或非全并发安全）
     ├─ 检查是否有 tool_calls → 有则 continue（下一轮）
     └─ 无 tool_calls → return（循环结束）
```

---

## 12. 关键不变量

### 12.1 协议层

| 不变量 | 含义 |
|---|---|
| **id / name 取首次非空** | 标准分片下它们只在首片出现，后续片的空值不能把已拿到的值覆盖掉 |
| **arguments 持续拼接** | 字符串分片必须累加，直到能 parse 成 dict |
| **index 区分多个 tool call** | 同一轮内并行多个 tool call 时，靠 `index` 分桶 |
| **usage 通常在末片** | 无 choices 的 chunk 上才出现，且需 `stream_options.include_usage=True` |
| **arguments 保持原始字符串** | 解析失败要能喂回模型，提前 parse 会把错误变成异常 |

### 12.2 流式派发层

| 不变量 | 含义 |
|---|---|
| **三个就绪条件** | 已出现更高 index + name 非空 + arguments 可 parse 成 dict |
| **一旦误判已派发，后续分片告警** | `fragment arrived after emitted` 日志 |
| **并发前缀 + 顺序其余** | CC `partitionToolCalls` 语义：安全前缀立即派发，首个非安全后全推迟 |
| **logical_key 稳定** | `native:turn:N:call:M`，attempt 重放落同一 ToolExecution |

### 12.3 异常与恢复层

| 不变量 | 含义 |
|---|---|
| **emitted 守卫** | 一旦吐过内容绝不重试 stream_options 降级，避免重复输出 |
| **stream_options 单次降级** | `_include_usage = False` 整进程记住，避免每轮都试错 |
| **ContextOverflowError 单独类型** | 触发压缩后重来一轮，而不是直接失败 |
| **体积兜底** | 400 + 请求体积逼近窗口 → 按超长处理，避免关键词漏判 |
| **reactive 单次守卫** | `max_reactive_compacts=1` 防死循环 |

### 12.4 诚实边界

- **provider 切换需重跑探针**：`scripts/probe_dashscope_tool_stream.py` 的结论只对实际打到的那个 provider 成立，换 provider 必须重跑。
- **字符估算有误差**：provider 不支持 `include_usage` 时，compact 阈值只能用字符估算，日志标 `estimated=true`。
- **Arguments 不 parse**：`ToolCall.arguments` 是字符串，由 `executor.parse_arguments()` 在工具执行前解析，失败时喂回模型。
- **provider 无关的体积兜底**：关键词漏判时，`_classify` 还有体积兜底判据，但**只对请求体积已逼近窗口的情况成立**。

---

## 参考阅读

- `agent/engine/native_loop/llm_client.py` —— 流式客户端 + 累积器 + 异常分类
- `agent/engine/native_loop/loop.py` —— 主循环 + `_build_request` + `_call_events`
- `agent/engine/native_loop/messages.py` —— 消息模型 + `to_wire` 协议转换
- `agent/engine/native_loop/tools.py` —— `ToolRegistry.wire_declarations()`
- `agent/engine/native_loop/executor.py` —— 工具执行 + `parse_arguments`
- `scripts/probe_dashscope_tool_stream.py` —— 真实 chunk 形状探针（换 provider 必跑）
- `agent/llm/exceptions.py` —— 共享异常分类 + `CONTEXT_OVERFLOW` 关键词
- [NativeLoop上下文压缩全链路详解.md](./NativeLoop上下文压缩全链路详解.md) —— 上游压缩机制
