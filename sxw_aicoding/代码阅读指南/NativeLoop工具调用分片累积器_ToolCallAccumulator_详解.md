# NativeLoop 工具调用分片累积器 `_ToolCallAccumulator` 详解

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

> `_ToolCallAccumulator` 位于 `agent/engine/native_loop/llm_client.py`，它的任务是将 OpenAI-compatible provider 的 ToolCall delta 分片，收敛成有界、可校验的 `ToolCall`。它不是工具执行器，也不是 Tool Broker。

## 1. 为什么 ToolCall 不能看到一个 chunk 就执行

provider 常见的工具流不是一个完整 JSON 对象，而是：

```text
chunk 1: index=0, id="call_a", name="knowledge_search", arguments="{\"query\":\"Na"
chunk 2: index=0, id=null,     name=null,               arguments="tive Loop\"}"
chunk 3: index=1, id="call_b", name="calculator",       arguments="{\"expr\":"
chunk 4: index=1, id=null,     name=null,               arguments="\"1+2\"}"
finish:  tool_calls
```

可以直接拼接的只有 `arguments` 字符串。id/name 通常只在首片出现，不同 call 依靠 `index` 区分。因此需要一个按 index 组织的局部状态，并且必须等待 provider 的显式 finish marker，才能证明默认生产模式下的整个 batch 已完整。

## 2. 它在完整链路中的位置

```text
Provider chunks
  → NativeLlmClient._consume
       ├─ 校验 choice / fragment 字段类型
       ├─ _ToolCallAccumulator.add(...)
       ├─ take_ready(allow_early=...)
       └─ 显式 finish + EOF 后 take_remaining()
            → ToolCallReady
                 → NativeLoop.run
                      ├─ 赋稳定 logical_key
                      ├─ 检查 Run/turn 上限
                      ├─ 校验工具存在与 JSON Schema
                      ├─ checkpoint / Broker PREPARE
                      └─ execute / settle / ToolResult
```

累积器的边界很刻意：

- 它识别 provider fragment 协议和字节上限。
- 它不查 ToolCatalog，不做 JSON Schema 校验，不决定 effect class。
- 它不创建 ToolExecution，不执行工具，不提交事件。
- `finish_reason` 与最终 batch 是否矛盾，由 `NativeLoop._validate_finish_reason` 在完整 turn 上判定。

生产 Adapter 以 `NativeLoop.run(initial_state=state)` 驱动 kernel；普通子 Runner 则传 `messages=...`。`run()` 要求恰好一个输入源，不会再用 `initial_state or LoopState(messages)` 静默决定哪份历史有效。这与 accumulator 一样遵守“临时 fragment 不得覆盖持久恢复状态”的边界。

## 3. 数据结构

### 3.1 `_PartialCall`：单个 index 的累积状态

```text
index       provider 给出的 call ordinal
id          首个非空 id
name        首个非空 function name
arguments   按到达顺序追加的原始 JSON 字符串
emitted     是否已产出过 ToolCallReady
```

`arguments` 故意保留为原始字符串，不在累积阶段转 dict。原因是：

- JSON 可能尚未收完。
- 最终 JSON 不合法是“模型可修正”错误，需要保留原文用于配对的 synthetic ToolResult。
- Runtime stable slot 会对 normalized arguments 计算 digest，这属于完整 batch/Broker 边界，不应在半截 fragment 上做。

### 3.2 `_ToolCallAccumulator`：整个 provider turn 的多槽位状态

```text
_calls                         dict[index, _PartialCall]
_max_index                     截止当前见过的最大 index
_max_calls                     每轮 call 数上限
_max_argument_bytes            单 call arguments 字节上限
_max_batch_argument_bytes      全 batch arguments 字节上限
_batch_argument_bytes          截止当前收到的分片字节总和
```

这个对象是每次 `_consume` 新建，不跨 provider request 共享，也不是 checkpoint 状态。只有完整接受的 `ToolCall` 才会进入 `LoopState.messages` 和 checkpoint。

## 4. `_consume` 先做线协议类型校验

调用 `add` 之前，`NativeLlmClient._consume` 已经检查：

- 一个 chunk 最多只能有一个 choice。
- choice index 只能是 0。
- finish marker 之后不能再有 choice data。
- ToolCall `index` 必须是非 bool 的整数。
- id/name/arguments 如果非 `None`，必须是字符串。

`add` 内仍检查 `index >= 0`。这种分工使累积器不需要猜 provider SDK 对象的类型，只处理已解包的基本字段。

## 5. `add(...)`：分片如何合并

每个 fragment 的处理顺序是：

```text
检查 index 非负
  → 如是新 index，检查每轮 call 数上限并创建 PartialCall
  → 计算本 arguments fragment 的 UTF-8 bytes
  → 检查该 call 累计 arguments 上限
  → 检查整批 arguments 上限
  → 如该 slot 已 emitted 且又有新内容，fail closed
  → merge id/name/arguments
  → 更新 batch bytes 和 max_index
```

### 5.1 id/name 是“首次非空后冻结”

`_PartialCall.merge` 不会让后续空值覆盖首片信息。如后续再出现非空 id/name：

- 值与原值相同，可接受。
- 值改变，立即 `MODEL_PROTOCOL_INVALID`。

这防止 provider 把同一 index 在中途重解释为另一个工具调用。

### 5.2 arguments 只能按顺序追加

```text
partial.arguments += fragment
```

不会对 fragment 做 JSON merge，也不会自动补大括号。provider 给出的字节序列是唯一事实；任何“智能修复”都可能把一个请求改成另一个请求。

### 5.3 大小按 UTF-8 bytes，不按 Python 字符数

默认限制：

| 资源 | 默认 | 错误码 |
|---|---:|---|
| 每轮 ToolCall 数 | 64 | `TOOL_CALL_LIMIT_EXCEEDED` |
| 单 call arguments | 64 KiB | `TOOL_ARGUMENTS_TOO_LARGE` |
| 全 batch arguments | 256 KiB | `TOOL_BATCH_TOO_LARGE` |

中文、emoji 等字符的 UTF-8 长度可大于 1，所以用 `len(text.encode("utf-8"))`。限制在收流过程中就执行，防止先无界累积再事后拒绝。

## 6. `take_remaining()`：默认生产路径的完整性边界

`_consume` 在 provider HTTP stream 结束后先确认：

```text
saw_choice == true
finish_seen == true
finish_reason != null
```

否则报 `MODEL_STREAM_INCOMPLETE`，绝不把自然 EOF 合成 `TurnEnd`。通过这一检查后，`take_remaining()` 再做最终形状校验：

1. 已出现的 indexes 必须精确等于 `[0, 1, ..., n-1]`。
2. 每个 slot 必须有非空 id 和 name。
3. 已在实验模式 emitted 的 slot 不重复产出。
4. 其余 slot 按 index 排序转成 `ToolCallReady`。
5. 全部 ToolCallReady 之后才产出唯一 `TurnEnd`。

在默认 `native_early_tool_dispatch=off` 下，`take_ready` 永返回空列表，因此所有 call 都只能在显式 finish 后由 `take_remaining` 产出。这是“模型 finish 前零工具执行”的第一层保证。

## 7. `take_ready()`：仅实验模式启用的启发式信号

### 7.1 判定条件

只有 `allow_early=true` 时，一个 slot 才可能提前产出。它必须同时满足：

```text
partial.emitted == false
partial.index < max_index            # 已经看到更高 index
partial.id 非空
partial.name 非空
json.loads(arguments) 是 dict
```

“看到更高 index”只是 provider 可能已离开前一 block 的启发式旁证；“JSON 已可解析”也不能证明 provider 不会再追加空格、额外字段或其他合法字符。所以这是实验机制，不是线协议事实。

最后一个 index 不会被该启发式提前产出，因为没有更高 index 作为旁证；它始终等到 `take_remaining()`。

### 7.2 `emitted` 不只是去重标记

提前产出时，累积器立即将 `partial.emitted = true`。之后如 provider 对该 index 又发出任何非空 id/name/arguments fragment，`add` 不会继续 merge，而是报：

```text
TOOL_REPLAY_MISMATCH
```

原因是该 ToolCall 可能已建立了稳定 Runtime slot，甚至已完成外部只读执行。继续修改参数就等于把一个已授权调用重解释为另一个调用，必须 fail closed。

## 8. 三种 early-dispatch 模式如何影响累积器

| 模式 | `take_ready` | 工具何时可执行 | 可靠性定位 |
|---|---|---|---|
| `off` | 永返回空 | 显式 finish、完整 batch 校验和 checkpoint/PREPARE 之后 | 生产默认 |
| `experimental_heuristic` | 使用更高 index + JSON object 启发式 | 安全只读前缀可提前 | 非生产保证 |
| `provider_block_complete` | 目标是使用 provider 明确 block 信号 | 当前 provider 不支持 | 选择后启动失败 |

当前 OpenAI-compatible client 的 `tool_call_block_complete` 为 `False`，Worker 会在 release 激活前以 `EARLY_DISPATCH_CAPABILITY_UNAVAILABLE` 失败，不会静默退化成启发式。

## 9. 实验提前产出后，kernel 和 Broker 还要做什么

`ToolCallReady` 不等于“可无条件执行”。`NativeLoop.run` 还会：

1. 根据 turn/call ordinal 赋稳定 `logical_key`。
2. 再检查每 turn、每 Run 数量和参数字节上限。
3. 查询冻结 ToolRegistry，只让 `concurrency_safe` 且无独占资源的工具继续。
4. 确认工具是已评审 READ_ONLY；UNKNOWN/副作用工具等完整 batch。
5. 解析 arguments 为 object 并通过 Draft 2020-12 schema。
6. 通过 Broker 为该 stable slot 先 PREPARE ToolCall 事实，再 `execute_prepared`。

实验调度器使用：

- 固定数量 worker task；
- 有界 `asyncio.Queue`；
- `submit()` await 入队，队列满时把反压传回 provider 消费；
- seal/join/cancel 明确生命周期；
- cancel、EOF、GeneratorExit、Runtime 故障时取消并 await 所有 worker/future。

流结束后，完整 batch 仍要经过 finish 语义校验和整批校验。后续 full-batch PREPARE 对 stable slot 做幂等核对；任何 name/request digest 漂移都以 `TOOL_REPLAY_MISMATCH` 终止，不允许用 provider call id 或 args hash 猜测另一个 identity。

正常工具的 `TOOL_CALL_COMMITTED` 现在只由 Broker PREPARE 产生。kernel 中无生产调用点的 `_call_events` 已删除，不再存在一条可被误解为工具权威的并行投影路径。只有默认 `off` 下整批零 dispatch 的模型可修正错误，才由 `_synthetic_events` 在 checkpoint 事务中提交成对 call/result。

工具执行层只会把普通工具业务异常投影为模型可见错误。`RuntimeFault` 和 `AttemptOwnershipLost`（如 stable-slot 漂移、契约失败、lease/fencing/CAS 丢失）原样透传，不会被 accumulator 或 Native executor 伪装成 ToolResult。

## 10. 默认 `off` 模式的完整示例

假设 provider 先给 call 0，再给 call 1：

```text
add(0, call_a, search, '{"query":"Na')
  calls[0].arguments = '{"query":"Na'
  take_ready(false) = []

add(0, null, null, 'tive"}')
  calls[0].arguments = '{"query":"Native"}'
  take_ready(false) = []

add(1, call_b, calculator, '{"expr":"1+2"}')
  take_ready(false) = []

provider finish_reason = tool_calls
HTTP stream closes normally
  → verify explicit finish
  → take_remaining()
       indexes == [0, 1]
       both have id/name
       returns [call_a, call_b]
  → yield ToolCallReady(call_a)
  → yield ToolCallReady(call_b)
  → yield TurnEnd(tool_calls)
```

kernel 得到完整 batch 后才会：

```text
校验 finish/batch
  → MODEL_RESPONSE_COMMITTED
  → Broker atomic PREPARE
  → TOOL_BATCH_COMMITTED
  → dispatch
```

所以即使 call 0 的 JSON 很早就可解析，默认生产路径也不会提前执行。

## 11. 边界情况与错误归属

| 情况 | 发现者 | 结果 |
|---|---|---|
| index 是 bool/字符串 | `_consume` | `MODEL_PROTOCOL_INVALID` |
| index < 0 | accumulator | `MODEL_PROTOCOL_INVALID` |
| indexes 是 `[0, 2]` | `take_remaining` | `MODEL_PROTOCOL_INVALID` |
| 缺 id 或 name | `take_remaining` | `MODEL_PROTOCOL_INVALID` |
| 同 index 的 id/name 改变 | `_PartialCall.merge` | `MODEL_PROTOCOL_INVALID` |
| 超过每轮 call 数 | `add` / kernel | `TOOL_CALL_LIMIT_EXCEEDED` |
| 单 call arguments 超限 | `add` / kernel | `TOOL_ARGUMENTS_TOO_LARGE` |
| batch arguments 超限 | `add` / kernel | `TOOL_BATCH_TOO_LARGE` |
| 只有 usage chunk / 自然 EOF | `_consume` | `MODEL_STREAM_INCOMPLETE` |
| 实验 emitted 后追加 fragment | `add` | `TOOL_REPLAY_MISMATCH` |
| JSON 不合法/非 object | kernel batch validator | 默认 `off`：整批零 dispatch，配对错误结果 |
| 工具不在冻结目录 | kernel batch validator | 默认 `off`：整批零 dispatch，`TOOL_NOT_FOUND` |
| JSON 不符合工具 schema | kernel batch validator | 默认 `off`：整批零 dispatch，`TOOL_ARGUMENTS_INVALID` |
| stable slot 重放时 name/digest 变化 | Broker boundary | `TOOL_REPLAY_MISMATCH` |

在 `experimental_heuristic` 下，完整 batch 校验失败前可能已有安全 READ_ONLY 前缀逐 slot PREPARE 并执行。此时系统会停止后续派发并 fail-closed；已完成的只读工作允许被浪费，但不能改绑或重解释。

最后三类错误不属于累积器，这是理解代码时最常见的边界混淆。

## 12. 累积器与 checkpoint 的关系

累积器本身不进 checkpoint。它只存活于一次 provider stream 的进程内存中。

正确的崩溃语义是：

- 拉流前已持久 `MODEL_REQUEST`。
- 累积到一半时崩溃，半截 `_PartialCall` 不是权威事实，不恢复它。
- 新 attempt 在同一稳定 message slot 上创建新 generation，重新请求 provider。
- 只有显式 finish 后接受的完整 assistant ToolCall batch，才进入 `MODEL_RESPONSE_COMMITTED`。
- 完整 stable slots PREPARE 后进入 `TOOL_BATCH_COMMITTED`。

这一设计避免了为 provider 临时 fragment 格式发明 checkpoint 兼容层，也避免恢复时猜测半截 JSON 的意图。

## 13. 关键不变量

1. 分片必须按 index 归槽，arguments 只按到达顺序追加。
2. id/name 一旦获得就不得改变。
3. 数量和参数体积在收流时就有界，大小按 UTF-8 bytes 计算。
4. 没有显式 finish，就不得产生完整 `TurnEnd`。
5. 默认 `off` 时，任何 call 都不在 finish 前产出。
6. JSON 可解析只是实验启发式，不是 provider block-complete 证明。
7. 实验 call emitted 后的任何追加都必须 `TOOL_REPLAY_MISMATCH`，不能重解释。
8. accumulator 只处理 fragment 形状；ToolCatalog/schema/effect/Broker 属于下游严格边界。
9. 默认路径的工具顺序是：完整 batch → checkpoint → Broker PREPARE → checkpoint → dispatch。
10. 累积器是 attempt-local 临时状态，不是 Runtime authority。

## 14. 建议的阅读顺序

```text
agent/engine/native_loop/llm_client.py
  → StreamItem / _PartialCall / _ToolCallAccumulator
  → NativeLlmClient._consume
agent/engine/native_loop/loop.py
  → ToolCallReady 分支
  → _validate_finish_reason / _validate_tool_batch
agent/engine/native_loop/engine.py
  → prepare_batch / execute_one / run_calls
agent/runtime/adapters/brokered_tools.py
  → prepare_native_batch / build_brokered_native_registry
```

按这个顺序能清楚看到：“一个 JSON 看起来完整”到“一个工具被生产系统允许执行”之间，还有 finish、batch、catalog、schema、effect、stable slot、checkpoint 和 fencing 多道边界。
