# ADR-0002：Streaming Commit Boundary

- 状态：Accepted / Frozen
- 日期：2026-08-09

## Context

旧链路把 Engine `StreamEvent` 直接转 SSE，客户端断开会影响执行，且已发送 token 未必有持久化事实。`done`、`error`、EOF 还可能被误用为终态。

## Decision

### 1. Commit before publish

Engine 只能把事件草稿交给 Runtime EventSink。EventSink 成功提交 `run_events` 后，SSE 查询才可能读取它。任何未提交草稿、模型 token 或 request-local UI frame 都不得发布。

### 2. Delta 聚合

`OUTPUT_DELTA_COMMITTED` 以“100ms 或 UTF-8 2KiB，先到者触发”聚合；测试使用 FakeClock，不依赖真实 sleep。以下边界前必须强制 flush：

- message 切换；
- 完整 ToolCall batch；
- 任一 ToolResult；
- checkpoint/compact boundary；
- Engine stop/outcome；
- 任意 terminal transaction。

崩溃只允许丢失尚未达到提交边界的内存 buffer；已经通过 SSE 可见的 delta 必然可重放。

### 3. 三类事实严格分离

1. `OUTPUT_DELTA_COMMITTED`：可展示 partial，不进入后续语义历史；
2. `ASSISTANT_MESSAGE_COMMITTED`：完整回答，只能与成功 terminal 原子提交，才进入 conversation history；
3. 客户端 cursor：调用方本地观看位置，不写 Runtime DB。

失败 Run 的 partial delta 可被看到，但不得被编译到下一轮模型历史。

### 4. SSE replay/tail

- `after_seq` 与 `Last-Event-ID` 都表示最后处理过的 opaque seq；二者同时存在时必须按公开 API 的确定规则选取，不隐式相加。
- 每次短查询读取 `seq > cursor` 的 committed、可见事件；约每 250ms poll，不持有事务。
- visibility 过滤可造成 seq 跳号；客户端只要求可见事件无丢失、无重复。
- 每 15 秒允许发送无 seq heartbeat comment；它不是 Canonical Event，也不改变 cursor。
- 读取到已提交 `RUN_TERMINATED` 后关闭订阅。
- SSE 断开只停止观看，不取消 Worker/Run。

### 5. Terminal authority

不再发送 `done`。Canonical `RUN_TERMINATED` 投影为 SSE `terminal`。Engine 的 `COMPLETED/RETRYABLE_FAILURE/TERMINAL_FAILURE/WAITING_INPUT/CANCELLED` 只是 Coordinator 输入；旧 `done`、`error`、clean EOF、生成器退出或异常吞并都不能裁决 Run。

### 6. Skill UI

Skill UI frame 必须先作为 `SKILL_UI_FRAME_COMMITTED` 进入 Event Store，再投影为 SSE `skill_event`；易失队列只可作为 attempt-local 批处理手段，不是事实源。

## Consequences

- 客户端重连可以从任意已见 seq 继续。
- 首版只能声明 committed/AVAILABLE，不能声明 DELIVERED/ACKED。
- SQLite 写放大受聚合控制，代价是最多约 100ms/2KiB 的持久化粒度。

