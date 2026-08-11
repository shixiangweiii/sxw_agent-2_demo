# SSE 流式输出端到端全链路代码阅读指南

本文档梳理从前端 CreateRun 到 SSE 流式输出完成的完整链路，包含时序图、调用栈、源码位置索引，帮助理解 Runtime 的事件推送机制。

---

## 目录

- [1. 架构概览](#1-架构概览)
- [2. 全局时序图](#2-全局时序图)
- [3. 后端 SSE 端点](#3-后端-sse-端点)
- [4. 前端 SSE 消费](#4-前端-sse-消费)
- [5. 调用栈详解](#5-调用栈详解)
- [6. 关键设计问题解答](#6-关键设计问题解答)
- [7. 源码位置索引](#7-源码位置索引)
- [8. 阅读建议](#8-阅读建议)

---

## 1. 架构概览

### 1.1 SSE 推送模型

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                              浏览器 (前端)                                  │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  createRun() → watchRun() → consumeSse() → handleSseEvent()          │   │
│  │  └─ lastSeq 持久化到 localStorage，断线重连时续订                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                    GET /runs/{id}/events?after_seq=N
                    (长连接，流式推送)
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Runtime API 进程 (:8000)                            │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  stream_events() → generate() → list_events() → _sse() → yield      │   │
│  │  └─ 250ms 轮询 SQLite，推送 seq > cursor 的事件                      │   │
│  │  └─ 15s 无事件时发送 heartbeat comment                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                                  │ 短查询
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SQLite (runtime.db)                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  run_events 表: append-only, seq 单调递增                            │   │
│  │  └─ 只有 committed event 可被 SSE 读取                               │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────┬───────────────────────────────────────────┘
                                  │
                                  │ append events (同事务提交)
                                  │
┌─────────────────────────────────┴───────────────────────────────────────────┐
│                         Runtime Worker 进程                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  EngineAdapter → CommittedEventSink → io.emit() → store.append_events │   │
│  │  └─ 引擎执行过程中产出 event drafts，聚合后原子提交                    │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 核心设计原则

| 原则 | 体现 |
|---|---|
| **先 commit，后 SSE 可见** | 事件必须在 SQLite 事务提交后才能被 SSE 推送 |
| **cursor 是 opaque 单调位置** | `seq` 不是连续计数器，visibility 过滤会造成跳号 |
| **断开订阅 ≠ 取消 Run** | 关闭 SSE 只表示停止观看，Worker 继续执行 |
| **DOM 不是持久化投影** | 页面刷新后从 `after_seq=0` 重放重建 UI |

---

## 2. 全局时序图

### 2.1 CreateRun → SSE 完整时序

```text
    浏览器                  API进程(:8000)              SQLite              Worker
      │                         │                       │                    │
      │──POST /runs────────────>│                       │                    │
      │  {engine, input, ...}   │                       │                    │
      │                         │──admit 事务──────────>│                    │
      │                         │  INSERT runs/events   │                    │
      │                         │<─COMMIT───────────────│                    │
      │<─202 {run_id}───────────│                       │                    │
      │                         │                       │                    │
      │                         │                       │    (250ms轮询)     │
      │                         │                       │<─────claim_next────│
      │                         │                       │  UPDATE state=RUNNING
      │                         │                       │                    │
      │──GET /runs/{id}/events──>│                       │                    │
      │  ?after_seq=0           │                       │                    │
      │                         │──list_events─────────>│                    │
      │                         │<─返回 seq 1-4─────────│                    │
      │<─SSE id:1 event:run_status──│                    │                    │
      │<─SSE id:2 event:run_status──│                    │                    │
      │<─SSE id:3 event:run_status──│                    │                    │
      │<─SSE id:4 event:activity_status─│                │                    │
      │                         │                       │                    │
      │                         │                       │    (引擎执行)      │
      │                         │                       │                    │
      │                         │                       │<─append_events─────│
      │                         │                       │  OUTPUT_DELTA(seq=5)
      │<─SSE id:5 event:text────│                       │                    │
      │  data: {delta:"混合"}   │                       │                    │
      │                         │                       │                    │
      │                         │                       │<─append_events─────│
      │                         │                       │  OUTPUT_DELTA(seq=6)
      │<─SSE id:6 event:text────│                       │                    │
      │  data: {delta:"召回"}   │                       │                    │
      │                         │                       │                    │
      │                         │                       │<─append_events─────│
      │                         │                       │  TOOL_CALL(seq=7)
      │<─SSE id:7 event:tool_call─│                     │                    │
      │  data: {tool_name:"..."}│                       │                    │
      │                         │                       │                    │
      │                         │                       │<─append_events─────│
      │                         │                       │  TOOL_RESULT(seq=8)
      │<─SSE id:8 event:tool_result─│                   │                    │
      │  data: {result:"..."}   │                       │                    │
      │                         │                       │                    │
      │<─...────────────────────│                       │                    │
      │                         │                       │                    │
      │                         │                       │<─finalize──────────│
      │                         │                       │  RUN_TERMINATED
      │<─SSE id:N event:terminal─│                      │                    │
      │  terminal_status:SUCCEEDED                      │                    │
      │                         │                       │                    │
      │──连接关闭────────────────│                       │                    │
```

### 2.2 断线重连时序

```text
    浏览器                  API进程(:8000)              SQLite
      │                         │                       │
      │──GET /events?after_seq=0──>│                    │
      │<─SSE id:1─────────────────│                     │
      │<─SSE id:2─────────────────│                     │
      │                         │                       │
      │   (网络断开)             │                       │
      │──连接中断────────────────X                       │
      │                         │                       │
      │   (localStorage 保存 lastSeq=2)                 │
      │                         │                       │
      │   (750ms 后重连)         │                       │
      │──GET /events?after_seq=2──>│                    │
      │                         │──list_events─────────>│
      │                         │<─返回 seq > 2─────────│
      │<─SSE id:3─────────────────│                     │
      │<─SSE id:4─────────────────│                     │
      │                         │                       │
```

### 2.3 页面刷新重建时序

```text
    浏览器                  API进程(:8000)              SQLite
      │                         │                       │
      │   (页面刷新)             │                       │
      │   localStorage 有 run_id, lastSeq=5             │
      │                         │                       │
      │──GET /runs/{id}──────────>│                     │
      │<─{run, conversation_id, trace_id}─│             │
      │                         │                       │
      │   (关键：lastSeq 重置为 0，从 committed 重建)   │
      │──GET /events?after_seq=0──>│                    │
      │                         │──list_events─────────>│
      │                         │<─返回 seq 1-N─────────│
      │<─SSE id:1─────────────────│                     │
      │<─SSE id:2─────────────────│                     │
      │<─...─────────────────────│                      │
      │<─SSE id:N─────────────────│                     │
      │                         │                       │
      │   (DOM 重建完成，继续 tail)                     │
      │──GET /events?after_seq=N──>│                    │
      │   (续订新事件)            │                       │
```

---

## 3. 后端 SSE 端点

### 3.1 端点定义

**文件**: `agent/runtime/api/runs.py:272-316`

```python
@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    request: Request,
    after_seq: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
```

### 3.2 调用栈

```text
stream_events() runs.py:272
├─ get_run(run_id)  ← 404 检查，在 streaming response 前失败
├─ 解析 cursor
│   ├─ after_seq (query) 优先
│   └─ Last-Event-ID (header) 作为备选
└─ StreamingResponse(generate(), media_type="text/event-stream")
   └─ generate() runs.py:290
      │
      │  while True:
      ├─ list_events(run_id, after_seq=cursor, limit=500)
      │   └─ SELECT * FROM run_events WHERE run_id=? AND seq>? ORDER BY seq LIMIT 500
      │
      ├─ for event in events:
      │   ├─ cursor = event.seq
      │   ├─ yield _sse(event)
      │   └─ if event_type is RUN_TERMINATED: return
      │
      ├─ get_run(run_id)
      │   └─ if status in TERMINAL and not events: return
      │
      ├─ if time.monotonic() - last_write >= 15s:
      │   └─ yield ": heartbeat\n\n"
      │
      └─ await asyncio.sleep(250ms)
```

### 3.3 SSE 事件格式化

**文件**: `agent/runtime/api/runs.py:215-269`

```python
_SSE_EVENT_NAMES: dict[EventType, str] = {
    EventType.USER_MESSAGE_COMMITTED: "user_message",
    EventType.OUTPUT_DELTA_COMMITTED: "text",
    EventType.TOOL_CALL_COMMITTED: "tool_call",
    EventType.TOOL_RESULT_COMMITTED: "tool_result",
    EventType.MODEL_PLAN_UPDATED: "plan_step",
    EventType.SKILL_UI_FRAME_COMMITTED: "skill_event",
    EventType.CITATION_SET_COMMITTED: "citation",
    EventType.ASSISTANT_MESSAGE_COMMITTED: "assistant_message",
    EventType.RUN_STATUS_CHANGED: "run_status",
    EventType.ACTIVITY_STATUS_CHANGED: "activity_status",
    EventType.RUN_TERMINATED: "terminal",
}

def _sse(event: CanonicalEvent) -> str:
    body = {
        "schema_version": event.schema_version,
        "event_id": event.event_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "activity_id": event.activity_id,
        "tool_execution_id": event.tool_execution_id,
        "seq": event.seq,
        "event_type": event.event_type,
        "payload": event.payload,
        "payload_ref": event.payload_ref,
        "terminal_status": event.terminal_status,
        "occurred_at": ms_to_rfc3339(event.occurred_at),
        "release_fingerprint": event.release_fingerprint,
    }
    name = _SSE_EVENT_NAMES.get(event.event_type, event.event_type.lower())
    return (
        f"id: {event.seq}\n"           # seq 作为 opaque cursor
        f"event: {name}\n"             # 事件类型
        f"data: {json.dumps(body)}\n\n" # JSON payload
    )
```

---

## 4. 前端 SSE 消费

### 4.1 整体流程

**文件**: `web/app.js`

```text
handleSubmit() app.js:401
├─ indexSelectedDocuments()  ← 文档入库
├─ uploadImageArtifact()     ← 图片上传
├─ createRun(query, refs)    ← POST /runs
├─ appendMessage("assistant", "")  ← 创建消息容器
└─ watchRun(assistant)       ← 启动 SSE 消费
```

### 4.2 watchRun 循环

**文件**: `web/app.js:340-361`

```javascript
async function watchRun(assistant) {
  state.watching = true;
  refreshControls();
  
  while (state.watching && !state.terminal) {
    state.watchController = new AbortController();
    try {
      const response = await fetch(
        `/api/v1/runs/${state.runId}/events?after_seq=${state.lastSeq}`,
        { signal: state.watchController.signal }
      );
      if (!response.ok || !response.body) throw new Error(`SSE 订阅失败`);
      
      await consumeSse(response, assistant);
      
      // 连接断开但未终态：等待 500ms 后重连
      if (!state.terminal && state.watching) {
        await new Promise(resolve => setTimeout(resolve, 500));
      }
    } catch (error) {
      if (error.name === "AbortError") break;
      // 断线重连：750ms 后按 cursor 续订
      setStatus("订阅断开，正在按 cursor 重连...");
      await new Promise(resolve => setTimeout(resolve, 750));
    }
  }
  
  state.watchController = null;
  refreshControls();
}
```

### 4.3 consumeSse 流式解析

**文件**: `web/app.js:323-338`

```javascript
async function consumeSse(response, assistant) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    
    // 按 \n\n 分割 SSE 块
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      handleSseEvent(parseSseBlock(buffer.slice(0, boundary)), assistant);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
  }
}
```

### 4.4 parseSseBlock 解析

**文件**: `web/app.js:279-289`

```javascript
function parseSseBlock(block) {
  const event = { type: "message", id: null, data: "" };
  const data = [];
  
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("id:")) event.id = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) event.type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  
  event.data = data.join("\n");
  return event;
}
```

### 4.5 handleSseEvent 渲染

**文件**: `web/app.js:291-321`

```javascript
function handleSseEvent(event, assistant) {
  if (!event.data) return;
  
  let envelope;
  try { envelope = JSON.parse(event.data); } catch { return; }
  const payload = envelope.payload || {};
  
  // 更新 cursor (持久化到 localStorage)
  if (event.id) {
    state.lastSeq = Math.max(state.lastSeq, event.id);
    localStorage.setItem("sxw.last_seq", String(state.lastSeq));
  }
  
  // 按事件类型渲染
  if (event.type === "text") {
    // 增量追加文本
    assistant.body.textContent += payload.delta || "";
    scrollToBottom();
  } else if (event.type === "assistant_message") {
    // 完整覆盖（committed 语义消息）
    assistant.body.textContent = payload.text || "";
    scrollToBottom();
  } else if (event.type === "citation") {
    // 添加引用
    addCitations(assistant.node, payload.citations || payload.refs || []);
  } else if (["tool_call", "tool_result", "plan_step", "skill_event", "run_status", "activity_status"].includes(event.type)) {
    // 添加到过程面板
    addProcessItem(assistant.node, event.type, payload);
  } else if (event.type === "terminal") {
    // 终态处理
    state.terminal = true;
    state.watching = false;
    setStatus(`运行结束：${envelope.terminal_status}`, 
              envelope.terminal_status === "SUCCEEDED" ? "ok" : "bad");
    // 轨迹的根 span 在引擎收口时才落盘，等到终态再挂链接
    addTraceLink(assistant.node, state.traceId);
    refreshControls();
  }
}
```

### 4.6 页面刷新重建

**文件**: `web/app.js:450-476`

```javascript
async function resumeStoredRun() {
  if (!state.runId) return;
  
  try {
    const response = await fetch(`/api/v1/runs/${encodeURIComponent(state.runId)}`);
    if (!response.ok) return;
    const run = await response.json();
    
    el.conversationId.value = run.conversation_id;
    state.traceId = run.trace_id || "";
    
    // 关键：lastSeq 重置为 0，从 committed events 重建 UI
    // DOM 不是持久化投影，刷新后必须重放全部事件
    const previousCursor = state.lastSeq;
    state.lastSeq = 0;
    state.terminal = false;
    localStorage.setItem("sxw.last_seq", "0");
    
    const assistant = appendMessage("assistant", "");
    addProcessItem(assistant.node, "resume", {
      run_id: state.runId,
      after_seq: 0,
      previous_transport_cursor: previousCursor,
    });
    
    setStatus("从 committed events 重建上次 Run...");
    await watchRun(assistant);
  } catch { /* a stale local cursor is harmless */ }
}
```

---

## 5. 调用栈详解

### 5.1 后端：CreateRun → SSE 推送

```text
[API 进程]
create_run() runs.py:131
├─ AdmissionService.create() admission.py:50
│   └─ store.admit() store.py:386
│       └─ BEGIN IMMEDIATE 事务
│           ├─ INSERT runs (state=DISPATCH_PENDING)
│           ├─ INSERT activities (state=PENDING)
│           ├─ APPEND events (seq 1-4)
│           └─ COMMIT
└─ 返回 202 + run_id

[Worker 进程]
RuntimeWorker.run() dispatcher.py:57
├─ claim_next() store.py:721
│   └─ UPDATE activities SET state=CLAIMED
│   └─ UPDATE runs SET state=RUNNING
└─ RunCoordinator.execute_claim() coordinator.py:65
    └─ LegacyEngineAdapter.execute() legacy_engines.py:65
        └─ engine.run_stream(rc)
            └─ CommittedEventSink.emit() events.py
                └─ store.append_events() store.py:582
                    └─ BEGIN IMMEDIATE 事务
                        ├─ INSERT run_events
                        ├─ UPDATE runs.next_seq
                        └─ COMMIT
                            │
                            ▼ (API 进程轮询发现)
[API 进程 SSE]
stream_events() runs.py:272
└─ generate() runs.py:290
    └─ while True:
        ├─ list_events(after_seq=cursor)
        │   └─ SELECT seq > cursor
        └─ yield _sse(event)
```

### 5.2 前端：CreateRun → SSE 消费

```text
[浏览器]
handleSubmit() app.js:401
├─ indexSelectedDocuments() app.js:238
│   └─ POST /api/v1/documents/index
│   └─ waitForIndexJob() app.js:226
├─ uploadImageArtifact() app.js:270
│   └─ POST /api/v1/artifacts
├─ createRun(query, refs) app.js:363
│   └─ POST /api/v1/runs
│   └─ 保存 run_id, traceId, lastSeq=0
├─ appendMessage("assistant", "") app.js:64
└─ watchRun(assistant) app.js:340
    └─ while watching && !terminal:
        ├─ fetch(/events?after_seq=lastSeq)
        └─ consumeSse(response, assistant) app.js:323
            └─ while !done:
                ├─ reader.read()
                ├─ buffer += decode(value)
                └─ while boundary = buffer.indexOf("\n\n"):
                    ├─ parseSseBlock() app.js:279
                    └─ handleSseEvent() app.js:291
                        ├─ 更新 lastSeq → localStorage
                        ├─ event.type === "text" → 增量追加
                        ├─ event.type === "tool_call" → addProcessItem
                        └─ event.type === "terminal" → 关闭连接
```

---

## 6. 关键设计问题解答

### 6.1 为什么 SSE 是短轮询而不是 WebSocket？

| 设计选择 | 原因 |
|---|---|
| **SQLite 不支持跨进程通知** | API 进程无法被 Worker 主动唤醒，只能轮询 |
| **SSE 足够** | 单向推送场景，不需要双向通信 |
| **断线重连简单** | 按 `lastSeq` 续订即可，不需要复杂的状态同步 |
| **HTTP/2 多路复用** | 现代浏览器支持良好，长连接开销可控 |

### 6.2 为什么 cursor 是 opaque 而不是连续计数器？

**原因**: `seq` 是 SQLite 自增 ID，但 visibility 过滤会造成跳号。例如：
- seq=5 是 PRIVATE 事件（不对外）
- seq=6 是 PUBLIC 事件
- 客户端看到 4 → 6，中间有"空洞"

客户端只需把 `seq` 当作不透明单调位置，不假设连续性。

### 6.3 为什么断开订阅不取消 Run？

**设计原则**: 观看与执行解耦

- SSE 断开只表示客户端停止观看
- Worker 继续执行，事件继续 commit
- 客户端可按 `lastSeq` 重连续订
- 显式取消必须调用 `/cancel` API

### 6.4 为什么页面刷新要从 seq=0 重放？

**原因**: DOM 不是持久化投影

- `lastSeq` 是传输 cursor，不是已渲染 DOM 的快照
- 刷新后 DOM 清空，必须从 committed events 重建
- 即使 Run 已终态，也要重放全部事件才能还原完整 UI
- `previous_transport_cursor` 记录刷新前的 cursor，用于诊断

### 6.5 为什么需要 heartbeat？

**作用**: 保持连接活跃，防止代理/防火墙切断空闲连接

- 15秒无事件时发送 `: heartbeat\n\n`
- SSE comment 不携带 seq，不更新 cursor
- 客户端收到 heartbeat 只需忽略

---

## 7. 源码位置索引

### 7.1 后端

| 功能 | 文件 | 行号 |
|---|---|---|
| SSE 端点 | `agent/runtime/api/runs.py` | 272 |
| SSE 生成器 | `agent/runtime/api/runs.py` | 290 |
| 事件格式化 | `agent/runtime/api/runs.py` | 230 |
| 事件类型映射 | `agent/runtime/api/runs.py` | 215 |
| list_events 查询 | `agent/runtime/adapters/sqlite/store.py` | 562 |
| append_events 事务 | `agent/runtime/adapters/sqlite/store.py` | 582 |

### 7.2 前端

| 功能 | 文件 | 行号 |
|---|---|---|
| CreateRun | `web/app.js` | 363 |
| watchRun 循环 | `web/app.js` | 340 |
| consumeSse 流式解析 | `web/app.js` | 323 |
| parseSseBlock | `web/app.js` | 279 |
| handleSseEvent 渲染 | `web/app.js` | 291 |
| 页面刷新重建 | `web/app.js` | 450 |

### 7.3 Worker 侧事件产出

| 功能 | 文件 | 行号 |
|---|---|---|
| CommittedEventSink | `agent/runtime/application/events.py` | 1 |
| LegacyEngineAdapter | `agent/runtime/adapters/legacy_engines.py` | 65 |
| io.emit() | `agent/runtime/adapters/legacy_engines.py` | 168 |
| io.force_flush() | `agent/runtime/adapters/legacy_engines.py` | 164 |

---

## 8. 阅读建议

### 8.1 推荐阅读顺序

```text
1. agent/runtime/api/runs.py:272          ← SSE 端点入口
2. agent/runtime/api/runs.py:290          ← generate() 生成器
3. agent/runtime/api/runs.py:230          ← _sse() 格式化
4. web/app.js:340                         ← watchRun 循环
5. web/app.js:323                         ← consumeSse 流式解析
6. web/app.js:291                         ← handleSseEvent 渲染
7. agent/runtime/application/events.py    ← CommittedEventSink (Worker 侧)
```

### 8.2 调试技巧

```bash
# 查看 Run 的 events
curl -sS "http://127.0.0.1:8000/api/v1/runs/run_xxx/events?after_seq=0"

# 查看特定 seq 之后的事件
curl -sS "http://127.0.0.1:8000/api/v1/runs/run_xxx/events?after_seq=5"

# 使用 Last-Event-ID header
curl -sS -H "Last-Event-ID: 5" "http://127.0.0.1:8000/api/v1/runs/run_xxx/events"

# 直接查 SQLite
sqlite3 local_storage/runtime/runtime.db \
  "SELECT seq, event_type, payload_json FROM run_events WHERE run_id='run_xxx' ORDER BY seq;"
```

### 8.3 前端调试

```javascript
// 浏览器控制台查看 SSE 状态
console.log(state.runId, state.lastSeq, state.terminal);

// 查看 localStorage 持久化的 cursor
console.log(localStorage.getItem("sxw.last_seq"));

// 手动重连（调试用）
state.watching = true;
watchRun(document.querySelector(".message.assistant"));
```

---

## 附录：SSE 事件类型速查

| SSE event | 来源 EventType | 说明 |
|---|---|---|
| `user_message` | `USER_MESSAGE_COMMITTED` | 用户输入 |
| `text` | `OUTPUT_DELTA_COMMITTED` | 增量文本 |
| `assistant_message` | `ASSISTANT_MESSAGE_COMMITTED` | 完整回答 |
| `tool_call` | `TOOL_CALL_COMMITTED` | 工具调用 |
| `tool_result` | `TOOL_RESULT_COMMITTED` | 工具结果 |
| `plan_step` | `MODEL_PLAN_UPDATED` | 规划步骤 |
| `skill_event` | `SKILL_UI_FRAME_COMMITTED` | Skill UI 帧 |
| `citation` | `CITATION_SET_COMMITTED` | 引用 |
| `run_status` | `RUN_STATUS_CHANGED` | Run 状态变更 |
| `activity_status` | `ACTIVITY_STATUS_CHANGED` | Activity 状态变更 |
| `terminal` | `RUN_TERMINATED` | 终态 |

---

*文档生成时间: 2026-08-09*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
