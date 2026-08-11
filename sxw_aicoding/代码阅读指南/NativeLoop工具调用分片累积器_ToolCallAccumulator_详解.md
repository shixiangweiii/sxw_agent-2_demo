# NativeLoop 工具调用分片累积器 `_ToolCallAccumulator` 详解

本文档聚焦 `native_loop` 的流式模型客户端 `llm_client.py` 中，负责把 LLM 返回的 **tool_call 分片（fragment）** 聚合成 **完整可执行工具调用** 的核心组件 `_ToolCallAccumulator`。文档会结合 `llm_client.py` 的上下游完整链路，说明它是如何处理单个、多个、交错、提前就绪等各种形态的。

---

## 目录

- [1. 为什么需要一个专门的累积器](#1-为什么需要一个专门的累积器)
- [2. 关键源码位置索引](#2-关键源码位置索引)
- [3. 核心数据结构](#3-核心数据结构)
- [4. 分片聚合流程](#4-分片聚合流程)
- [5. 多个 function call 的处理](#5-多个-function-call-的处理)
- [6. 提前就绪判定与流式工具执行](#6-提前就绪判定与流式工具执行)
- [7. 与上下游的交互](#7-与上下游的交互)
- [8. 边界情况与防御](#8-边界情况与防御)
- [9. 关键不变量](#9-关键不变量)

---

## 1. 为什么需要一个专门的累积器

`native_loop` 直接使用 `openai.AsyncOpenAI.chat.completions.create(stream=True)` 发起 SSE 流式调用。LLM 在流式输出 function call 时，不会像普通 JSON 那样一次性给出完整参数，而是把每个 tool_call 拆成多个 chunk 分片返回：

| chunk | index | id | name | arguments |
|---|---|---|---|---|
| 1 | 0 | `call_abc` | `calculator` | `""` |
| 2 | 0 | `None` | `None` | `{"expr` |
| 3 | 0 | `None` | `None` | `ession":"1` |
| 4 | 0 | `None` | `None` | `2*12"}` |
| 5 | 1 | `call_def` | `get_weather` | `""` |
| 6 | 1 | `None` | `None` | `{"city` |
| 7 | 1 | `None` | `None` | `":"杭州"}` |

因此必须有一个中间层：
1. **按 `index` 分槽**，把属于同一个 tool_call 的分片拼到一起；
2. **把 `arguments` 字符串片段累加成完整 JSON**；
3. **`id` / `name` 只取首次非空值**，避免被后续空分片覆盖；
4. **支持一次 turn 返回多个 tool_call**；
5. **支持流式工具执行**：在参数完整且安全时提前判定就绪，不等整轮流完。

这个中间层就是 `_ToolCallAccumulator`。

---

## 2. 关键源码位置索引

| 模块 | 文件路径 | 关键符号 |
|---|---|---|
| 流式客户端 | `agent/engine/native_loop/llm_client.py` | `NativeLlmClient`, `_consume`, `_ToolCallAccumulator`, `_PartialCall`, `TextDelta`, `ToolCallReady`, `TurnEnd` |
| 主循环 | `agent/engine/native_loop/loop.py` | `NativeLoop.run`, `_execute`, `_call_events`, `_result_events` |
| 消息模型 | `agent/engine/native_loop/messages.py` | `ToolCall`, `Msg`, `to_wire` |
| 工具执行 | `agent/engine/native_loop/executor.py` | `run_calls`, `execute_one`, `parse_arguments` |
| 探针脚本 | `scripts/probe_dashscope_tool_stream.py` | 真实 chunk 形状实测 |

---

## 3. 核心数据结构

### 3.1 `_PartialCall`：单个 tool_call 的累积状态

```python
# agent/engine/native_loop/llm_client.py:88-118
@dataclass
class _PartialCall:
    index: int
    id: str = ""
    name: str = ""
    arguments: str = ""
    emitted: bool = False

    def merge(self, call_id: Any, name: Any, arguments: Any) -> None:
        # id/name 取首次非空值：标准分片下它们只在首片出现，
        # 后续片的空值不能把已拿到的值覆盖掉。
        if call_id and not self.id:
            self.id = str(call_id)
        if name and not self.name:
            self.name = str(name)
        if arguments:
            self.arguments += str(arguments)

    def is_parseable(self) -> bool:
        """参数能解析成 JSON 对象 —— 提前判定就绪的安全闸。"""
        text = self.arguments.strip()
        if not text:
            return False
        try:
            return isinstance(json.loads(text), dict)
        except (TypeError, ValueError):
            return False

    def to_call(self) -> ToolCall:
        return ToolCall(id=self.id, name=self.name, arguments=self.arguments)
```

每个 `_PartialCall` 维护一个槽位的状态：
- `index`：在 SSE chunk 中的位置，区分多个 tool_call；
- `id` / `name`：首次非空写入，后续忽略空值；
- `arguments`：持续追加字符串片段；
- `emitted`：是否已经作为 `ToolCallReady` 产出过，防止重复产出；
- `is_parseable()`：检查当前 `arguments` 是否能解析成 JSON 对象，是提前就绪的安全闸；
- `to_call()`：把累积状态转换为上层使用的 `ToolCall` 对象。

### 3.2 `_ToolCallAccumulator`：多槽位累积器

```python
# agent/engine/native_loop/llm_client.py:120-164
class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[int, _PartialCall] = {}
        self._max_index: int = -1
```

- `_calls`：`index → _PartialCall` 的映射，一个 turn 内可同时存在多个槽位；
- `_max_index`：当前见过的最大 `index`，用于判断某个低 index 的 tool_call 是否已收完（出现更高 index 意味着它已经结束）。

---

## 4. 分片聚合流程

### 4.1 添加分片：`add(...)`

```python
# agent/engine/native_loop/llm_client.py:125-136
    def add(self, index: int, call_id: Any, name: Any, arguments: Any) -> None:
        partial = self._calls.get(index)
        if partial is None:
            partial = _PartialCall(index=index)
            self._calls[index] = partial
        if partial.emitted and arguments:
            # 已判定就绪后又收到该 index 的新分片：说明上游是交错分片，
            # 提前就绪的判断在这种形态下不成立。响亮记一条，便于排障。
            log_kv(logger, logging.WARNING, "NativeLlm",
                   "fragment arrived after tool call was emitted", index=index)
        partial.merge(call_id, name, arguments)
        self._max_index = max(self._max_index, index)
```

流程：
1. 按 `index` 找到或创建 `_PartialCall`；
2. 如果该槽位已经 `emitted` 且又来了新的 `arguments`，打 warning（说明提前就绪判断在这种 provider 分片形态下不适用）；
3. 调用 `partial.merge(...)` 合并 `id/name/arguments`；
4. 更新 `_max_index`。

### 4.2 合并规则：`merge(...)`

```python
# agent/engine/native_loop/llm_client.py:96-104
    def merge(self, call_id: Any, name: Any, arguments: Any) -> None:
        if call_id and not self.id:
            self.id = str(call_id)
        if name and not self.name:
            self.name = str(name)
        if arguments:
            self.arguments += str(arguments)
```

核心逻辑：
- `id` 和 `name` 只取**首次非空值**；
- `arguments` 是**字符串追加**。

这兼容两种 provider 形态：
- **标准 OpenAI 分片**：首片带 `id + name`，后续片只追加 `arguments`；
- **一次性完整分片**：单一片段就带完整 `id + name + arguments`，直接合并即可。

### 4.3 最终兜底：`take_remaining()`

```python
# agent/engine/native_loop/llm_client.py:156-164
    def take_remaining(self) -> list[ToolCall]:
        remaining: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted:
                continue
            partial.emitted = True
            remaining.append(partial.to_call())
        return remaining
```

在 SSE 流结束时调用，按 `index` 顺序把所有未 emit 的槽位全部产出。这是保证不丢 tool_call 的最后一道防线。

---

## 5. 多个 function call 的处理

`_ToolCallAccumulator` 从设计之初就是面向多个 function call 的：

1. **分槽存储**：`_calls: dict[int, _PartialCall]` 为每个 `index` 维护独立状态；
2. **同时累积**：同一轮流式输出中，`index=0` 和 `index=1` 的分片会并行进入各自的槽位；
3. **按 index 顺序产出**：`take_ready()` 和 `take_remaining()` 都按 `sorted(self._calls)` 遍历，保证产出顺序稳定；
4. **max_index 辅助判断**：`_max_index` 记录当前见过的最大 index，用于判断低 index 的 tool_call 是否已经收完。

示例：如果一轮返回两个 tool_call，累积器内部状态变化如下：

```text
chunk #1  idx=0 id=call_a name=calc args=""
    _calls = {0: _PartialCall(index=0, id="call_a", name="calc", arguments="")}

chunk #2  idx=0 id=None name=None args="{\"x\":1"
    _calls = {0: _PartialCall(index=0, id="call_a", name="calc", arguments="{\"x\":1")}

chunk #3  idx=1 id=call_b name=weather args=""
    _calls = {
        0: _PartialCall(index=0, id="call_a", name="calc", arguments="{\"x\":1"),
        1: _PartialCall(index=1, id="call_b", name="weather", arguments=""),
    }
    _max_index = 1

chunk #4  idx=0 id=None name=None args=",\"y\":2}"
    arguments 变为 "{\"x\":1,\"y\":2}"，且 index=0 < _max_index=1
    如果开启 allow_early，此时 index=0 判定为就绪，产出 ToolCallReady(call_a)

chunk #5  idx=1 id=None name=None args="{\"city\":\"杭州\"}"
    arguments 完整，流结束时 take_remaining 产出 ToolCallReady(call_b)
```

---

## 6. 提前就绪判定与流式工具执行

### 6.1 为什么需要提前就绪？

`native_loop` 支持**流式工具执行（streaming tool execution）**：一个 tool_call 的参数一旦完整，不等整轮流完就立刻派发执行，从而降低端到端延迟。但前提必须是**参数真的完整了**，否则拿到半截 JSON 就去执行会失败。

### 6.2 `take_ready(...)` 的判定条件

```python
# agent/engine/native_loop/llm_client.py:138-154
    def take_ready(self, *, allow_early: bool) -> list[ToolCall]:
        if not allow_early:
            return []
        ready: list[ToolCall] = []
        for index in sorted(self._calls):
            partial = self._calls[index]
            if partial.emitted:
                continue
            # 仅当"已出现更高 index"且"参数已能解析"时才判定就绪。
            if index < self._max_index and partial.name and partial.is_parseable():
                partial.emitted = True
                ready.append(partial.to_call())
        return ready
```

判定一个 tool_call 可以提前就绪，必须同时满足：
1. **`allow_early=True`**：上游调用方决定是否需要提前派发；
2. **`index < self._max_index`**：已经出现更高 index 的 tool_call，说明当前 tool_call 已经结束（不会再有新的分片）；
3. **`partial.name` 非空**：已经拿到函数名；
4. **`partial.is_parseable()`**：`arguments` 能解析成 JSON 对象，确保参数完整。

其中 `is_parseable()` 是关键安全闸，防止半截参数被误判为完整。

### 6.3 提前就绪的收益与风险

- **收益**：在多个 tool_call 的场景下，低 index 的工具可以率先开始执行，不必等高 index 全部传完；
- **风险**：某些 provider 的分片形态可能不是严格的 index 顺序递增，如果低 index 在判定为就绪后还有新分片，代码会打 warning 并继续累积，但最终该 tool_call 已经被 emit 出去，后续分片不会被合并——这是已知限制，靠 warning 暴露问题。

---

## 7. 与上下游的交互

### 7.1 上游：`_consume(...)` 调用累积器

```python
# agent/engine/native_loop/llm_client.py:274-319
async def _consume(self, payload: dict[str, Any], allow_early: bool) -> AsyncIterator[StreamItem]:
    accumulator = _ToolCallAccumulator()
    finish_reason: Optional[str] = None
    usage: Optional[Usage] = None

    async with await self._client.chat.completions.create(**payload) as stream:
        async for chunk in stream:
            # ... 解析 usage / finish_reason / delta ...

            content = getattr(delta, "content", None)
            if content:
                yield TextDelta(content)

            for raw in getattr(delta, "tool_calls", None) or []:
                index = getattr(raw, "index", None)
                function = getattr(raw, "function", None)
                accumulator.add(
                    0 if index is None else int(index),
                    getattr(raw, "id", None),
                    getattr(function, "name", None) if function else None,
                    getattr(function, "arguments", None) if function else None,
                )
            for call in accumulator.take_ready(allow_early=allow_early):
                yield ToolCallReady(call)

    for call in accumulator.take_remaining():
        yield ToolCallReady(call)
    yield TurnEnd(finish_reason=finish_reason, usage=usage)
```

`_consume` 的职责：
1. 发起 SSE 流；
2. 对每个 chunk，把文本产出为 `TextDelta`；
3. 对每个 tool_call 分片，调用 `accumulator.add(...)`；
4. 每次 add 之后，调用 `accumulator.take_ready(...)`，把已就绪的 tool_call 产出为 `ToolCallReady`；
5. 流结束后，调用 `accumulator.take_remaining()` 兜底产出所有剩余 tool_call；
6. 最后产出 `TurnEnd` 表示本轮结束。

### 7.2 更上游：`NativeLlmClient.stream(...)` 封装

```python
# agent/engine/native_loop/llm_client.py:191-272
async def stream(
    self,
    *,
    messages: list[Msg],
    tools: Optional[list[dict[str, Any]]] = None,
    allow_early_tool_dispatch: bool = True,
    temperature: float = 0.2,
) -> AsyncIterator[StreamItem]:
    # 1. 组装 payload
    # 2. 打开 llm span
    # 3. 调用 _consume 迭代
    # 4. 异常分类 + stream_options 降级重试
```

`stream(...)` 是比 `_consume` 更外层的包装：
- 构造 OpenAI 请求体；
- 打开 trace span；
- 调用 `_consume` 并产出 `StreamItem`；
- 处理 `stream_options` 不被支持的降级重试；
- 对异常进行分类（如上下文超长）。

### 7.3 下游：`NativeLoop.run(...)` 消费 `StreamItem`

```python
# agent/engine/native_loop/loop.py
async for item in self._client.stream(...):
    if isinstance(item, TextDelta):
        # 累积文本增量，准备输出给用户
    elif isinstance(item, ToolCallReady):
        # 收集 tool_call，同一轮多个 tool_call 会批量并发执行
    elif isinstance(item, TurnEnd):
        # 本轮结束，根据 finish_reason 决定下一步
```

下游主循环：
- 把连续的 `TextDelta` 聚合成 assistant message；
- 把同一轮内连续出现的多个 `ToolCallReady` 收集成一个批次，调用 `executor.run_calls(...)` 并发执行；
- 根据 `TurnEnd.finish_reason` 决定是继续下一轮、输出最终结果还是终止。

---

## 8. 边界情况与防御

| 场景 | 处理逻辑 | 位置 |
|---|---|---|
| `index` 为 `None` | 回退到 `0` | `_consume(...)` |
| `id/name` 后续片为空 | 只取首次非空，不覆盖 | `_PartialCall.merge(...)` |
| `arguments` 为空字符串 | 跳过追加 | `_PartialCall.merge(...)` |
| 单分片完整 tool_call | 直接合并，流结束时 `take_remaining` 产出 | `_ToolCallAccumulator.add(...)` / `take_remaining(...)` |
| 多个 tool_call | 按 `index` 分槽，分别累积 | `_calls: dict[int, _PartialCall]` |
| 提前就绪后又来同 index 分片 | 打 warning，提示提前就绪判断不适用该 provider 分片形态 | `_ToolCallAccumulator.add(...)` |
| 参数不是合法 JSON | `is_parseable()` 返回 False，不允许提前就绪 | `_PartialCall.is_parseable(...)` |
| 整流结束仍有未 emit 的槽位 | `take_remaining()` 兜底全部产出 | `_ToolCallAccumulator.take_remaining(...)` |
| `allow_early=False` | `take_ready()` 直接返回空列表，所有 tool_call 等流结束才产出 | `_ToolCallAccumulator.take_ready(...)` |

---

## 9. 关键不变量

1. **一个 `index` 只对应一个 `_PartialCall`**。同一轮内相同 `index` 的分片必须属于同一个 tool_call。
2. **`id` / `name` 一旦写入不会被后续空值覆盖**。这依赖于 OpenAI 兼容协议“首片带 id/name”的约定。
3. **已 `emitted` 的槽位不会再被 `take_ready` / `take_remaining` 产出**。防止同一个 tool_call 被重复派发。
4. **提前就绪必须同时满足：出现更高 index、有 name、参数可解析为 JSON 对象**。三者缺一不可。
5. **`take_remaining()` 保证所有累积的 tool_call 最终都会被产出**。即使提前就绪逻辑有遗漏，流结束时也会兜底。
6. **产出顺序按 `index` 升序**。保证上层按 LLM 原本意图处理多个 tool_call。

---

## 总结

`_ToolCallAccumulator` 是 `native_loop` 与 OpenAI 兼容流式协议之间的关键适配层：

- 它以 **`index` 分槽**的方式天然支持一次 turn 返回多个 function call；
- 通过 **`merge` 的“首非空 + 字符串追加”规则**兼容标准分片和一次性完整分片两种 provider 形态；
- 通过 **`is_parseable()` 安全闸 + `_max_index` 结束判断**支持流式工具执行的提前派发；
- 通过 **`take_remaining()` 兜底**保证任何情况下不丢 tool_call。

理解了它，就能明白 `native_loop` 是如何把原始 SSE chunk 一步步转换成上层可消费的 `TextDelta` / `ToolCallReady` / `TurnEnd` 的。
