# SSE 保活机制详解

本文以当前 `agent/runtime/api/runs.py`、`web/app.js` 和 `eval/harness/sse_client.py` 为准，说明无业务事件时的 SSE heartbeat、连接收口与断线重连。

## 1. Heartbeat 解决的问题

CreateRun 已 durable accepted，但下一条公开事件可能需要很久：

- Worker 还在等待 exact release claim；
- provider 首 token 延迟较高；
- 工具或 Skill 正在外部执行；
- Run 等待人工输入；
- 公开事件之间只有 INTERNAL 诊断事件。

如果 HTTP 响应长时间无字节，中间代理、NAT 或客户端可能把连接当成空闲链路。Runtime 因此在未终态且无业务事件时发送 SSE comment。

Heartbeat 只证明当前 HTTP/SSE 响应仍在产生字节；它不证明 Worker 健康、引擎正在前进，也不代表 Run 成功。

## 2. 服务端循环

SSE endpoint 位于：

```text
GET /api/v1/runs/{run_id}/events
```

启动 streaming response 前，API 先 `get_run(run_id)`，让不存在的 Run 以正常 404 结束，而不是在 SSE body 开始后才失败。

generator 的简化逻辑是：

```python
cursor = initial_cursor
last_write = time.monotonic()

while True:
    events = await store.list_events(run_id, after_seq=cursor, limit=500)
    for event in events:
        cursor = event.seq
        yield encode_sse(event)
        last_write = time.monotonic()
        if event is RUN_TERMINATED:
            return

    run = await store.get_run(run_id)
    if run is terminal and not events:
        return

    if time.monotonic() - last_write >= heartbeat_seconds:
        yield ": heartbeat\n\n"
        last_write = time.monotonic()

    await asyncio.sleep(poll_ms / 1000)
```

默认配置：

| 配置 | 默认值 | 含义 |
|---|---:|---|
| `runtime_sse_poll_ms` | 250 ms | 无新事件时的短查询间隔 |
| `runtime_sse_heartbeat_seconds` | 15 s | 距离最后一次实际 SSE write 的保活间隔 |

`last_write` 在发送业务事件或 heartbeat 时更新，所以业务流量持续时不会额外穿插 heartbeat。

## 3. Heartbeat 的线上格式

```text
: heartbeat

```

实际字节是：

```python
": heartbeat\n\n"
```

SSE 规范中，以 `:` 开头的行是 comment。它与业务 event 的根本差异是：

| 属性 | 业务 Canonical Event | heartbeat comment |
|---|---|---|
| 是否写入 `run_events` | 是 | 否 |
| 是否有 `id`/seq | 是 | 否 |
| 是否有 `event`/`data` | 是 | 否 |
| 是否推进 reconnect cursor | 是 | 否 |
| 是否改变 Run 状态 | 可能 | 否 |
| 是否参与 replay | 是 | 否 |

不要给 heartbeat 分配假 seq，也不要把它记成 Canonical Event；否则会污染 Run 事实流和 cursor 语义。

## 4. cursor 初始化和断线续传

API 支持两种 cursor：

```text
?after_seq=<n>
Last-Event-ID: <n>
```

规则是：

1. query `after_seq` 存在时优先；
2. 否则尝试解析 `Last-Event-ID`；
3. header 非法时安全回到 0；
4. `list_events()` 只返回 `seq > cursor` 的已提交公开事件。

heartbeat 没有 seq，因此在 heartbeat 后断线，客户端仍使用最后一个业务 event id 重连。这不会漏事件，也不需要服务端为 heartbeat 记住任何状态。

## 5. 终态如何关闭连接

有两个安全收口：

### 5.1 流中读到 `RUN_TERMINATED`

API 先 yield 这条 terminal event，然后立即 return。客户端因此能先处理权威终态，再看到 response EOF。

### 5.2 Run 已终态且 cursor 之后无事件

这覆盖客户端从 terminal seq 之后误连或重连的情况。API 不会在已终态 Run 上无限 heartbeat。

必须注意：**SSE EOF 本身不是 Run 终态权威。** 客户端应以 `terminal` 事件或 GET Run status 为准；网络断开只表示订阅失效。

## 6. Web UI 如何忽略 heartbeat

`web/app.js` 手工解析 SSE block。对 `: heartbeat`：

- parser 不会得到 id/event/data 字段；
- `handleSseEvent()` 发现 `event.data` 为空后直接返回；
- `lastSeq`、回答正文、过程卡片和 Run terminal 都不变。

这是正确行为：comment 的作用是让网络链路有字节，不是驱动 UI。

## 7. Eval harness 如何忽略 heartbeat

`eval/harness/sse_client.py` 只处理 `event:` 和 `data:` 行。comment 行不会设置 `event_type`，所以到空行边界时不调用 `_dispatch()`。

断线时 harness 以最后业务 seq 同时填入 query 和 `Last-Event-ID` 重连。由于 query 优先，两者值一致时语义明确。

## 8. 与 Native 背压的关系

Native 直接 awaited RuntimeIO 的背压发生在 Worker 写 Runtime Store 的路径；heartbeat 发生在 API 读 Runtime Store 的路径。二者彼此独立：

- Worker 提交慢：Native 不继续拉 provider；API 仍可在无新事件时发 heartbeat。
- 客户端断开：API 的 subscription 结束，Worker Run 继续；不会把 GeneratorExit 传给 EngineAdapter。
- API 重启：新 API 进程从 SQLite committed events 按 cursor replay，不需要恢复旧进程内 SSE queue。

## 9. HTTP 响应头与代理

SSE response 设置：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

`no-transform` 避免中间层改写事件流，`X-Accel-Buffering: no` 用于提示兼容代理不要缓冲整段响应。但 heartbeat 间隔仍应小于真实部署中最短的代理 idle timeout；这是部署参数，不能只靠代码默认值猜测。

## 10. 常见误区

### 误区 1：收到 heartbeat 就说明 Worker 正在执行

不对。heartbeat 由 API subscription 协程产生，与 Worker heartbeat/release readiness 是两套机制。

### 误区 2：heartbeat 可以作为 cursor

不对。它没有 seq，重连必须使用最后已处理的业务 event id。

### 误区 3：断开 SSE 可以取消 Run

不对。断开只停止读投影；取消必须发送显式 cancel command。

### 误区 4：只要 response EOF，Run 就结束了

不对。终态只看 `RUN_TERMINATED` 或 GET Run status。未看到 terminal 就 EOF 应视为传输中断并按 cursor 重连。

## 11. 源码阅读索引

- `agent/runtime/api/runs.py`：SSE 编码、cursor、poll、heartbeat 和 terminal 收口。
- `agent/config.py`：`runtime_sse_poll_ms` / `runtime_sse_heartbeat_seconds`。
- `web/app.js`：`parseSseBlock()`、`handleSseEvent()`、`watchRun()`。
- `eval/harness/sse_client.py`：按 committed cursor 的消费与重连。
- `tests/reliability/test_runtime_api.py`：cursor 优先级、heartbeat 与 replay 契约。
