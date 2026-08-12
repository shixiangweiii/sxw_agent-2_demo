# SSE 保活机制详解

本文档梳理 SSE 连接在无事件时的保活机制、heartbeat 设计、连接生命周期管理。

---

## 目录

- [1. 核心问题](#1-核心问题)
- [2. 服务端保活机制](#2-服务端保活机制)
- [3. Heartbeat 详解](#3-heartbeat-详解)
- [4. 连接生命周期](#4-连接生命周期)
- [5. 前端处理](#5-前端处理)
- [6. 源码位置索引](#6-源码位置索引)

---

## 1. 核心问题

### 1.1 问题场景

```text
前端 CreateRun 后立即订阅 SSE：
  GET /runs/{id}/events?after_seq=0

此时可能的情况：
  1. Worker 还没领取 Claim，没有新事件
  2. Worker 正在执行引擎，但模型响应慢，还没产出事件
  3. Worker 执行工具调用，工具执行时间长

问题：SSE 连接会因为没有数据而卡住吗？会超时断开吗？
```

### 1.2 答案

**服务端发送 heartbeat 保持未终态连接；连接不会因为暂时没有业务事件而主动断开，Run 进入终态后会按协议结束。**

---

## 2. 服务端保活机制

### 2.1 SSE 生成器

**文件**: `agent/runtime/api/runs.py:290-307`

```python
async def generate():
    cursor = initial_cursor
    last_write = time.monotonic()  # 记录最后一次写入时间
    
    while True:
        # 1. 查询新事件（短查询，limit=500）
        events = await store.list_events(run_id, after_seq=cursor, limit=500)
        
        # 2. 推送事件
        for event in events:
            cursor = event.seq
            yield _sse(event)
            last_write = time.monotonic()  # 更新写入时间
            if event.event_type is EventType.RUN_TERMINATED:
                return  # 终态，关闭连接
        
        # 3. 检查 Run 是否已终态
        run = await store.get_run(run_id)
        if run.status in TERMINAL_RUN_STATUSES and not events:
            return  # Run 已终态且无新事件，关闭连接
        
        # 4. Heartbeat：超过 15 秒无数据，发送 comment
        if time.monotonic() - last_write >= settings.runtime_sse_heartbeat_seconds:
            yield ": heartbeat\n\n"  # ← SSE comment
            last_write = time.monotonic()  # 重置写入时间
        
        # 5. 轮询间隔 250ms
        await asyncio.sleep(settings.runtime_sse_poll_ms / 1000)
```

### 2.2 保活流程图

```text
┌─────────────────────────────────────────────────────────────┐
│                    SSE generate() 循环                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  while True:                                                │
│    │                                                        │
│    ├─ 1. list_events(after_seq=cursor)                      │
│    │   ├─ 有事件 → yield 事件 → 更新 last_write             │
│    │   └─ 无事件 → 继续                                     │
│    │                                                        │
│    ├─ 2. 检查终态                                           │
│    │   └─ run.status in TERMINAL and not events → return    │
│    │                                                        │
│    ├─ 3. Heartbeat 检查                                     │
│    │   └─ time.monotonic() - last_write >= 15s              │
│    │       └─ yield ": heartbeat\n\n" → 重置 last_write     │
│    │                                                        │
│    └─ 4. sleep(250ms)                                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Heartbeat 详解

### 3.1 Heartbeat 格式

```text
: heartbeat\n\n
```

- 以 `:` 开头 → SSE comment
- 客户端自动忽略，不触发任何事件
- 不携带 `id:` → 不更新 cursor
- 不携带 `event:` → 不触发 onmessage

### 3.2 为什么用 SSE Comment？

| 设计选择 | 原因 |
|---|---|
| **SSE Comment** | 标准 SSE 协议支持，客户端自动忽略 |
| **不携带 seq** | 不是真实事件，不更新 cursor |
| **不触发事件** | 避免客户端误处理 |
| **保持连接** | 代理/防火墙看到数据流，不会切断 |

### 3.3 Heartbeat 时序

```text
时间轴 (秒)    事件
────────────────────────────────────────────
0.0            连接建立，推送 seq 1-4
0.0            last_write = 0.0
5.0            轮询，无事件，last_write 仍为 0.0
10.0           轮询，无事件，last_write 仍为 0.0
15.0           time.monotonic() - last_write = 15s
15.0           yield ": heartbeat\n\n"
15.0           last_write = 15.0
20.0           轮询，无事件
30.0           time.monotonic() - last_write = 15s
30.0           yield ": heartbeat\n\n"
30.0           last_write = 30.0
...            (循环)
45.0           Worker 产生新事件
45.0           yield SSE id:5 event:text
45.0           last_write = 45.0
```

### 3.4 配置参数

```python
# .env 默认值
RUNTIME_SSE_HEARTBEAT_SECONDS = 15   # heartbeat 间隔（秒）
RUNTIME_SSE_POLL_MS = 250            # 轮询间隔（毫秒）
```

---

## 4. 连接生命周期

### 4.1 连接关闭条件

| 情况 | 触发条件 | 代码位置 |
|---|---|---|
| **收到终态事件** | `event_type is RUN_TERMINATED` | `runs.py:299-300` |
| **Run 已终态且无新事件** | `run.status in TERMINAL and not events` | `runs.py:302-303` |
| **客户端主动断开** | 浏览器关闭/网络断开/AbortController | 客户端行为 |
| **代理/防火墙超时** | 无 heartbeat 超过代理超时时间 | 网络基础设施 |

### 4.2 连接不会关闭的情况

| 情况 | 说明 |
|---|---|
| **Worker 未领取 Claim** | 持续轮询 + heartbeat |
| **Worker 执行中但无事件** | 持续轮询 + heartbeat |
| **模型响应慢** | 持续轮询 + heartbeat |
| **工具执行时间长** | 持续轮询 + heartbeat |

### 4.3 完整生命周期时序

```text
    浏览器                  API进程(:8000)              SQLite              Worker
      │                         │                       │                    │
      │──GET /events?after_seq=0──>│                    │                    │
      │                         │──list_events─────────>│                    │
      │                         │<─返回 seq 1-4─────────│                    │
      │<─SSE id:1─────────────────│                     │                    │
      │<─SSE id:2─────────────────│                     │                    │
      │<─SSE id:3─────────────────│                     │                    │
      │<─SSE id:4─────────────────│                     │                    │
      │                         │                       │                    │
      │                         │                       │    (Worker 未领取)  │
      │                         │                       │                    │
      │                         │   (轮询...)           │                    │
      │                         │   (无事件)            │                    │
      │                         │                       │                    │
      │<─": heartbeat\n\n"────────│   (15秒后)          │                    │
      │   (客户端忽略)            │                       │                    │
      │                         │                       │                    │
      │                         │                       │    (Worker 领取)    │
      │                         │                       │<─claim_next────────│
      │                         │                       │                    │
      │                         │   (轮询...)           │                    │
      │                         │   (无事件)            │                    │
      │<─": heartbeat\n\n"────────│   (15秒后)          │                    │
      │                         │                       │                    │
      │                         │                       │    (引擎执行)      │
      │                         │                       │                    │
      │                         │                       │<─append_events─────│
      │                         │──list_events─────────>│                    │
      │<─SSE id:5 event:text────│                       │                    │
      │                         │                       │                    │
      │                         │   (继续轮询...)       │                    │
      │<─": heartbeat\n\n"────────│   (无事件时)        │                    │
      │                         │                       │                    │
      │                         │                       │<─finalize──────────│
      │                         │──list_events─────────>│                    │
      │<─SSE id:N event:terminal─│                      │                    │
      │                         │                       │                    │
      │──连接关闭────────────────X                       │                    │
```

---

## 5. 前端处理

### 5.1 前端 SSE 消费

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

### 5.2 Heartbeat 在前端的处理

```javascript
// app.js:279-289
function parseSseBlock(block) {
  const event = { type: "message", id: null, data: "" };
  const data = [];
  
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("id:")) event.id = Number(line.slice(3).trim());
    else if (line.startsWith("event:")) event.type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    // 注意：以 ":" 开头的行被忽略（SSE comment）
  }
  
  event.data = data.join("\n");
  return event;
}
```

**Heartbeat 处理**：
- `: heartbeat\n\n` → `parseSseBlock` 返回 `{type: "message", id: null, data: ""}`
- `handleSseEvent` 检查 `if (!event.data) return;` → 直接返回，不处理

### 5.3 前端断线重连

```javascript
// app.js:340-361
async function watchRun(assistant) {
  state.watching = true;
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
}
```

---

## 6. 源码位置索引

| 功能 | 文件 | 行号 |
|---|---|---|
| SSE 生成器 | `agent/runtime/api/runs.py` | 290 |
| Heartbeat 逻辑 | `agent/runtime/api/runs.py` | 304-306 |
| 终态检查 | `agent/runtime/api/runs.py` | 301-303 |
| 配置参数 | `agent/config.py` | (搜索 SSE) |
| 前端 SSE 消费 | `web/app.js` | 323 |
| 前端 Heartbeat 处理 | `web/app.js` | 279 |
| 前端断线重连 | `web/app.js` | 340 |

---

## 附录：关键配置

| 参数 | 默认值 | 说明 |
|---|---|---|
| `RUNTIME_SSE_HEARTBEAT_SECONDS` | 15 | 无业务事件多久后发送 heartbeat |
| `RUNTIME_SSE_POLL_MS` | 250 | 轮询间隔（毫秒） |

---

## 常见问题

### Q1: Heartbeat 会不会影响 cursor？

**A**: 不会。Heartbeat 是 SSE comment（以 `:` 开头），不携带 `id:`，客户端不会更新 `lastSeq`。

### Q2: 如果代理超时时间小于 15 秒怎么办？

**A**: 可以调小 `RUNTIME_SSE_HEARTBEAT_SECONDS`，比如设为 5 秒。

### Q3: Heartbeat 会不会被误认为是事件？

**A**: 不会。SSE 协议规定以 `:` 开头的行是 comment，客户端自动忽略。

### Q4: 为什么不用 WebSocket？

**A**: SSE 足够满足单向推送需求，且断线重连更简单（按 cursor 续订即可）。WebSocket 需要双向通信，增加复杂度。

---

*文档生成时间: 2026-08-12*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
