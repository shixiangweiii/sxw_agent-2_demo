# ADR-0003：SQLite Authority 与单机边界

- 状态：Accepted / Frozen
- 日期：2026-08-09

## Context

目标是个人学习和架构验证所需的“单机持久化可靠 Agent Runtime 参考实现”，而不是分布式 HA。需要一个能真实实现事务、CAS、唯一约束和进程重启恢复，同时不过早引入 PostgreSQL/Temporal 兼容层的方案。

## Decision

### 1. 技术与物理边界

- 使用 `aiosqlite + 显式 SQL`，不引入 ORM/Alembic。
- 领域/application 只依赖 Store/UoW 端口；但首版不预留第二 backend 或最低公分母兼容层。
- `local_storage/runtime/runtime.db` 是 Runtime 权威。
- `local_storage/arag/rag.db` 是 Document/version/index-job/chunk 权威。
- `local_storage/artifacts/sha256/...` 是 Artifact 完整字节权威，数据库保存 metadata/link。
- `local_storage/traces/...` 仅为诊断事实。
- 数据库必须位于本机磁盘，禁止 NFS、共享盘或云同步目录。

### 2. 每连接 PRAGMA

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

写操作使用短 `BEGIN IMMEDIATE`；SSE 长连接只做短读，不持有事务。API 与一个 Worker 进程可以并发访问；Worker 内默认有限并发 4。首版不宣称多机或高并发分布式队列能力。

### 3. Schema authority

`schema_migrations` 保存 version、SQL checksum、applied_at。启动时顺序应用已知 migration；发现数据库含未知新版本、同版本 checksum 被改写或缺失中间版本，必须 fail-fast。禁止自动猜测/修补。

`run_events` 由唯一约束和触发器禁止 update/delete。`projection_cursors` 只在真实物化投影存在时创建；不创建空表。明确不创建 `delivery_cursors`，SSE 读取不写数据库。

### 4. Authority 与投影

vector/BM25 是可重建投影，不是 Document truth；ADK Session/native messages 是 attempt adapter，不是 history truth；Trace 是诊断，不是恢复来源。损坏或缺失投影时从权威数据重建并返回 `DEGRADED`，不得反向采用索引/缓存内容更新权威。

### 5. 故障声明

WAL + `synchronous=FULL` 支撑正常进程崩溃后的本地恢复，但不承诺主机丢失、磁盘损坏、外部误删或站点级容灾。备份只能用 SQLite backup API 或正确 checkpoint 流程，不能在活跃 WAL 时只复制主 DB 文件并宣称完整。

## Rejected alternatives

- **立即 PostgreSQL**：增加部署和运维成本，不能替代状态机、幂等和恢复设计。
- **立即 Temporal**：会掩盖本轮要显式学习/验证的 Tool effect、checkpoint 与所有权问题。
- **同时抽象 SQLite/PostgreSQL backend**：本轮没有真实第二实现，预留会扩大测试面并形成虚假兼容承诺。
- **Trace/JSONL 作恢复日志**：无业务事务、CAS 和唯一 terminal 约束。

## Consequences

项目可以承诺单机进程级恢复和确定性事务语义；不得表述为分布式 HA、磁盘容灾或任意数量 Worker 的生产队列。

