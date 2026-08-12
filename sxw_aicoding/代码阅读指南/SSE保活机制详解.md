# SSE 保活机制详解

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

本文以 `agent/runtime/api/runs.py::stream_events` 为准。SSE heartbeat 是 API subscription 的保活字节，不是 Worker heartbeat、Run progress 或终态。

## 1. 为什么需要它

Run 已 durable accepted 后，可能暂时没有公开 event：它也许在等待 exact-release Worker、模型首 token、工具/Skill、重试或人工信号。若 HTTP 长时间无字节，代理/NAT/客户端可能把连接当作空闲。API 因此在无输出时发送 SSE comment。

这不能证明正确 Worker 正在运行。Worker readiness 是另一套机制：本次启动后的新鲜 `ACTIVE` heartbeat，且它的三引擎 release map 必须和 `active_releases` 精确一致。

## 2. 服务端循环

endpoint 在 streaming response 前先 `get_run(run_id)`，让不存在的 Run 以 404 结束。cursor 规则是 query `after_seq` 优先，否则解析 `Last-Event-ID`，非法 header 回退到 0。

```python
cursor = initial_cursor
last_write = time.monotonic()
while True:
    events = await store.list_events(run_id, after_seq=cursor, limit=500)
    for event in events:
        cursor = event.seq
        yield encode_sse(event)
        last_write = time.monotonic()
        if event.event_type is RUN_TERMINATED:
            return
    if run_is_terminal_and_no_events:
        return
    if time.monotonic() - last_write >= heartbeat_seconds:
        yield ': heartbeat\\n\\n'
        last_write = time.monotonic()
    await asyncio.sleep(poll_ms / 1000)
```

默认 `runtime_sse_poll_ms` 为 250ms，`runtime_sse_heartbeat_seconds` 为 15s。业务 event 或 heartbeat 都会更新 `last_write`，所以业务流量持续时不会额外插入保活 comment。

## 3. heartbeat 的语义

实际格式是：

```text
: heartbeat

```

| 属性 | committed Canonical Event | heartbeat |
|---|---|---|
| 写入 `run_events` | 是 | 否 |
| `id`/seq | 是 | 否 |
| `event`/`data` | 是 | 否 |
| 推进 cursor | 是 | 否 |
| replay | 是 | 否 |
| 改变 Run | 可能 | 否 |

不要给 heartbeat 分配假 seq。它既不参与 generation，也不会使 buffered Worker text 变得可见；后者仍必须等 RuntimeIO/Store commit。

## 4. 终态、断线和 Native 背压

读取到 `RUN_TERMINATED` 时，API 先输出 terminal block 再 return；若 Run 已终态且 cursor 后没有 event，也立即结束，不会无限 heartbeat。EOF 本身仍不是业务终态：客户端只应相信 terminal event 或 GET Run status；没收到 terminal 的 EOF 视为传输中断并按最后业务 seq 重连。

Native 的 backpressure 发生在 Worker：每个 provider/kernel event 必须先 `await RuntimeIO.emit` 才能拉下一项。text 的 durable flush 仍遵守 100ms/2KiB 与身份/Tool/checkpoint/close 边界，而 heartbeat 是 API 的只读循环。客户端断开只停止这一轮读取，既不会取消 Run，也不会向 Adapter 传播取消。

## 5. 客户端和部署

`web/app.js` 的 SSE parser 对 comment 不产生 data，因此不改 `lastSeq` 或 UI。`eval/harness/sse_client.py` 同样只处理完整 `event:`/`data:` block，重连时带最后业务 seq。

response 设置 `Content-Type: text/event-stream`、`Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no`。实际部署应把 heartbeat 间隔配置得小于最短代理 idle timeout；默认值不是任何网络环境的保证。

## 6. 阅读索引

- `agent/runtime/api/runs.py`：cursor、poll、heartbeat 和 terminal 收口。
- `agent/config.py`：SSE 配置。
- `web/app.js`、`eval/harness/sse_client.py`：消费与重连。
- `agent/runtime/worker/dispatcher.py`：与 SSE 无关的 Worker heartbeat。
