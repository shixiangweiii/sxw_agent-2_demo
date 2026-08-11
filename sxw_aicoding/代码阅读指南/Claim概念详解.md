# Claim 概念详解

本文档解释 Runtime 中 Claim 的含义、数据结构、领取机制和设计意图。

---

## 目录

- [1. 核心定义](#1-核心定义)
- [2. 数据结构](#2-数据结构)
- [3. Claim 与 Activity 的关系](#3-claim-与-activity-的关系)
- [4. 领取机制](#4-领取机制)
- [5. 设计意图](#5-设计意图)
- [6. 状态迁移](#6-状态迁移)
- [7. 源码位置索引](#7-源码位置索引)

---

## 1. 核心定义

**Claim = Worker 对一条 Activity 的排他性执行权**

Claim 是 Worker 通过 `claim_next()` 从 SQLite 领取的一条待执行 Activity，代表"这条工作单元已经被我认领了，别人别碰"的凭证。

---

## 2. 数据结构

**文件**: `agent/runtime/ports/store.py:66`

```python
@dataclass(frozen=True)
class Claim:
    run: RunRecord           # 要执行的 Run（包含 input_text、engine、deadline 等）
    activity: ActivityRecord # 被领取的 Activity（包含 attempt、fencing_token、lease 等）
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `run` | `RunRecord` | Run 的快照，包含执行所需的上下文：input_text、engine、deadline_at、release_fingerprint 等 |
| `activity` | `ActivityRecord` | Activity 的快照，包含执行权信息：attempt、fencing_token、lease_expires_at 等 |

---

## 3. Claim 与 Activity 的关系

### 3.1 区别

| 概念 | 是什么 | 生命周期 | 存储位置 |
|---|---|---|---|
| **Activity** | 数据库中的一行记录，代表"要执行的工作单元" | 持久化，可被多次领取（重试） | `activities` 表 |
| **Claim** | Worker 领取 Activity 后获得的"执行权凭证" | 内存中，Worker 崩溃就丢失 | Worker 进程内存 |

### 3.2 类比

```text
Activity = 餐厅里的一份订单（持久存在，可被多次查看）
Claim    = 厨师从订单架取走订单后，获得"这道菜由我做"的凭证（临时持有）

厨师崩溃了？凭证丢失，订单回到架上，其他厨师可以重新领取。
```

### 3.3 关系图

```text
┌─────────────────────────────────────────────────────────────┐
│                         Claim                                │
│  ┌─────────────────┐      ┌─────────────────────────────┐  │
│  │   run: RunRecord │      │  activity: ActivityRecord   │  │
│  │   (执行上下文)   │      │  (执行权 + 租约 + fencing)  │  │
│  └─────────────────┘      └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │                          │
         │                          │
         ▼                          ▼
   ┌───────────┐             ┌───────────────┐
   │  runs 表  │             │ activities 表 │
   │ (持久化)  │             │   (持久化)    │
   └───────────┘             └───────────────┘
```

### 3.4 关键点

1. **Activity 可以多次被 Claim** — 每次重试 `attempt+1`，但 Activity 行不变
2. **Claim 包含 Run 快照** — Worker 执行时需要 Run 的 input_text、engine、deadline 等
3. **Claim 附带 fencing token** — 证明"我是当前合法的执行者"
4. **Claim 有租约** — `lease_expires_at` 过期后，其他 Worker 可重新领取

---

## 4. 领取机制

### 4.1 claim_next 调用栈

**文件**: `agent/runtime/adapters/sqlite/store.py:721`

```text
claim_next() store.py:721
├─ 开启事务 (BEGIN IMMEDIATE)
│
├─ SELECT 符合条件的 Activity
│   WHERE a.type='ENGINE_RUN'
│     AND a.state='PENDING'           -- 待领取
│     AND a.available_at <= now       -- 到期可领取
│     AND (
│       r.state='DISPATCH_PENDING'    -- 普通待执行
│       OR (r.state='CANCEL_REQUESTED' AND ...)  -- 取消后的 reconcile
│     )
│     AND r.deadline_at > now         -- 未过期
│     AND r.engine IN (...)           -- Worker 支持的引擎
│   ORDER BY a.available_at, a.created_at  -- FIFO
│   LIMIT 1
│
├─ UPDATE activities SET
│   state='CLAIMED',
│   attempt=attempt+1,
│   lease_owner=worker_id,
│   lease_expires_at=now+lease_ms,
│   fencing_token=fencing_token+1,
│   revision=revision+1
│
├─ UPDATE runs SET state='RUNNING' (或保持 CANCEL_REQUESTED)
│
├─ APPEND events
│   ├─ ACTIVITY_STATUS_CHANGED: PENDING → CLAIMED
│   └─ RUN_STATUS_CHANGED: DISPATCH_PENDING → RUNNING (仅普通 claim)
│
└─ RETURN Claim(run, activity)
```

### 4.2 Worker 领取循环

**文件**: `agent/runtime/worker/dispatcher.py:57`

```python
async def run(self) -> None:
    while not self._stop.is_set():
        await self._maintenance(now)  # heartbeat + recover_expired
        
        while len(self._tasks) < self.concurrency:
            claim = await self.store.claim_next(
                worker_id=self.worker_id,
                lease_ms=self.lease_ms,
                now_ms=self.clock.now_ms(),
                engines=tuple(self.release_map),
            )
            if claim is None:
                break  # 没有待领取的
            
            task = asyncio.create_task(self._execute(claim))
            self._tasks.add(task)
        
        await asyncio.wait_for(self._stop.wait(), timeout=self.poll_ms / 1000)
```

### 4.3 原子性保证

```sql
-- 单事务内完成，多 Worker 竞争时只有一个成功
BEGIN IMMEDIATE;

-- Worker A 和 Worker B 同时执行到这一步
-- 但 UPDATE 的 WHERE state='PENDING' 保证只有一个能成功
UPDATE activities SET state='CLAIMED', lease_owner='worker-A'
WHERE activity_id='act_xxx' AND state='PENDING';

-- Worker B 的 UPDATE 影响 0 行，claim_next 返回 None
COMMIT;
```

---

## 5. 设计意图

### 5.1 解决的核心问题

| 问题 | Claim 的解决方式 |
|---|---|
| **多 Worker 竞争** | CAS + `fencing_token` 保证只有一个 Worker 胜出 |
| **Worker 崩溃** | lease 过期后，其他 Worker 可重新领取 |
| **过期执行者迟到提交** | `fencing_token` 校验，过期 token 被拒绝 |
| **取消与执行的竞态** | claim 时检查 Run 状态，cancel 后只走 reconcile 路径 |

### 5.2 为什么需要 fencing_token？

```text
时间线：
  t0: Worker-A claim Activity, fencing_token=1
  t1: Worker-A 崩溃，但 lease 还没过期
  t2: lease 过期，Worker-B claim 同一个 Activity, fencing_token=2
  t3: Worker-A 重启，带着旧的 fencing_token=1 尝试提交
  
结果：Store 校验 fencing_token，拒绝 Worker-A 的提交
      └─ "STALE_FENCING_TOKEN" 错误
```

### 5.3 为什么需要 lease？

```text
时间线：
  t0: Worker-A claim Activity, lease_expires_at=t0+30s
  t1: Worker-A 执行中，每 10s 续租
  t2: Worker-A 崩溃，无法续租
  t3: t0+30s 后，lease 过期
  t4: Worker-B claim 同一个 Activity，成功
  
结果：Activity 不会永远被"锁死"在崩溃的 Worker 手里
```

### 5.4 为什么 Claim 不持久化？

| 设计选择 | 原因 |
|---|---|
| **Claim 是内存中的** | 它只是"当前执行权"的快照，不需要持久化 |
| **Activity 是持久化的** | 它是工作单元的权威记录，需要跨 Worker 生命周期存在 |
| **崩溃恢复** | Worker 崩溃后，Claim 丢失，但 Activity 还在，其他 Worker 可重新领取 |

---

## 6. 状态迁移

### 6.1 Activity 状态

```text
PENDING ──claim_next──► CLAIMED ──mark_running──► RUNNING ──完成──► SUCCEEDED/FAILED
    ▲                                                      │
    │                                                      └──► 新建 PENDING (重试)
    │
    └─── lease 过期 + recover_expired
```

### 6.2 Run 状态

```text
DISPATCH_PENDING ──claim_next──► RUNNING ──┬──► SUCCEEDED
                                           │
                                           ├──► FAILED
                                           │
                                           ├──► CANCELLED
                                           │
                                           └──► TIMED_OUT
```

### 6.3 关键事件

| 事件 | 触发时机 |
|---|---|
| `ACTIVITY_STATUS_CHANGED: PENDING → CLAIMED` | Worker 领取成功 |
| `ACTIVITY_STATUS_CHANGED: CLAIMED → RUNNING` | Coordinator 开始执行 |
| `RUN_STATUS_CHANGED: DISPATCH_PENDING → RUNNING` | 普通 claim（非 reconcile） |

---

## 7. 源码位置索引

| 功能 | 文件 | 行号 |
|---|---|---|
| Claim 定义 | `agent/runtime/ports/store.py` | 66 |
| claim_next 方法 | `agent/runtime/adapters/sqlite/store.py` | 721 |
| Worker 领取循环 | `agent/runtime/worker/dispatcher.py` | 57 |
| Coordinator 执行（`_execute_claim`，`execute_claim` 在 65 行只做 trace_id 恢复后转发） | `agent/runtime/application/coordinator.py` | 76 |
| mark_activity_running | `agent/runtime/adapters/sqlite/store.py` | 807 |
| renew_lease | `agent/runtime/adapters/sqlite/store.py` | 829 |
| recover_expired | `agent/runtime/adapters/sqlite/store.py` | 3139 |

---

## 附录：常见疑问

### Q1: Claim 和 Activity 是一对一吗？

**A**: 是的，一个 Claim 对应一条 Activity。但一条 Activity 可能被多次 Claim（重试时）。

### Q2: Worker 崩溃后，Claim 怎么办？

**A**: Claim 是内存中的，崩溃就丢失。Activity 的 lease 过期后，其他 Worker 可以重新 claim。

### Q3: 多 Worker 同时 claim 同一条 Activity 会怎样？

**A**: SQLite 事务保证只有一个成功。其他 Worker 的 `UPDATE ... WHERE state='PENDING'` 影响 0 行，`claim_next` 返回 `None`。

### Q4: Claim 后 Worker 执行失败，Activity 会怎样？

**A**: 根据 `outcome.kind` 决定：
- `RETRYABLE_FAILURE` → `schedule_retry`，Activity 重新变为 PENDING
- `TERMINAL_FAILURE` → Run 终结，Activity 终结
- `COMPLETED` → Run 成功，Activity 成功

### Q5: Claim 的 fencing_token 和 Activity 的 fencing_token 是同一个吗？

**A**: 是的。Claim 时 `fencing_token+1`，Claim 对象里保存的是递增后的值。后续所有操作（mark_running、save_checkpoint、finalize）都要带上这个 fencing_token 校验。

---

*文档生成时间: 2026-08-09*
*基于项目版本: sxw_agent-2_demo R0 冻结规格*
