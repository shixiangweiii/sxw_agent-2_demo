# FastAPI `app.state` 依赖注入机制详解

> 文档基线：2026-08-12 当前项目源码；已删除的测试模块和门禁脚本不再作为行为依据。

## 1. 当前结论

`agent/main.py` 的 API 进程只把三个已组装的本进程对象放进 FastAPI `app.state`：

```text
app.state.settings
app.state.runtime_store
app.state.artifact_store
```

handler 通过 `request.app.state` 取引用并构造当次应用服务。`app.state` 不是共享状态、Worker registry、release authority 或跨进程 DI 容器；Run/release/event/checkpoint 的 authority 是 `runtime.db`，Artifact bytes 是 SHA-256 CAS。

## 2. lifespan 的实际边界

`lifespan()` 创建 `RuntimeDatabase`、`SqliteRuntimeStore`，先 `await store.initialize()`，再设置三项 state。只有 `yield` 前成功完成，FastAPI 才开始接收请求。

`initialize()` 调 `common/sqlite_schema.py::ensure_current_schema()`：空库在一个 `BEGIN IMMEDIATE` 中安装完整 current schema 并写 schema digest；非空库只接受与完整 `agent/runtime/adapters/sqlite/schema.sql` 原始字节 SHA-256 完全相同的 `schema_meta`。不匹配是 `CURRENT_SCHEMA_MISMATCH`，不会 migration、`ALTER`、覆盖或静默重建。

Store 的每次读写自行打开/关闭 SQLite connection；`app.state.runtime_store` 是 adapter 引用，不是一条跨请求长事务。

## 3. 三项对象分别做什么

| state | 职责 | 不是 |
|---|---|---|
| `settings` | API 配置快照、deadline/SSE/ARAG 地址等 | Worker 实际加载的 release |
| `runtime_store` | admission、状态、committed event、cancel/signal、Artifact metadata、active release 查询 | 内存中的 Run 副本 |
| `artifact_store` | SHA-256 CAS bytes 读写 | Artifact metadata authority |

上传先落入 CAS，再用 Store 登记 metadata；读取先核对 metadata，再按 digest 读取/校验 CAS。两者必须配合，不能以文件路径替代 authority。

## 4. handler 如何注入

`agent/runtime/api/runs.py` 以窄 `_store(request)` 取得 Store。CreateRun 读取 `request.app.state.settings`，每次构造轻量 `AdmissionService(store, default_deadline_ms=...)`；应用层依赖 `RuntimeStore` protocol，而不是自行创建 SQLite connection。

`agent/runtime/api/artifacts.py` 同时取 ArtifactStore 和 RuntimeStore。`agent/api/documents.py` 只从 settings 取 ARAG HTTP 配置，不在 Runtime API 装载 ARAG 索引。

`GET /healthz` 读取 `active_releases`。这仅说明 API 能读数据库，不能说明 Worker ready；完整 readiness 还需本次启动后的新鲜 `ACTIVE` Worker heartbeat，且 heartbeat 的三引擎 release map 与 active pointers 精确一致。

## 5. 与 Worker 是两条装配路径

```text
API lifespan
  -> settings + Store + ArtifactStore -> app.state -> HTTP handlers

Worker build_worker()
  -> settings + Store + ArtifactStore
  -> LLM / Skill / A2A / Claude Skill 工具源
  -> strict ToolCatalog + ToolBroker
  -> 2 AdkEngineAdapter + 1 NativeLoopAdapter
  -> activate_current_releases(三指针同一事务切换)
```

Worker 不读取 API 的 `app.state`，也不是 FastAPI 后台 task。它在所有 Adapter 成功后才发布三份 immutable release manifest；activation 会拒绝存在异 fingerprint 非终态 Run 的切换。admission 在自己的短事务中读取当前 pointer 并把 exact fingerprint 冻结到 Run，Worker claim 也精确匹配 `(engine, release_fingerprint)`。

两个 ADK Adapter 的 session/artifact service 都是 per-attempt 临时对象；NativeLoopAdapter 直连 RuntimeIO，使用严格 checkpoint、awaited event admission、Broker 和 explicit final assistant。它们都不应被放进 API `app.state`。

## 6. 启动验证和并发含义

判断 API 装配是否完成，应以 FastAPI lifespan 已成功越过 `store.initialize()`、三项 `app.state` 已注入以及 `/healthz` 能读取 active release 为准。`/healthz` 仍不是完整 Worker readiness；后者由 `scripts/run_all.sh` 结合新鲜 Worker heartbeat 与三份 exact release pointer 判断。

`app.state` 仅保存对象引用，不自动提供协程安全。当前 Store 用短 `BEGIN IMMEDIATE` 写事务和 fencing，CAS 用 digest/原子 rename。每个 API 进程有自己的 `app.state`；当前共享 SQLite/CAS 是本机多进程边界，不是跨节点 HA。

## 7. 阅读索引

- `agent/main.py`：lifespan、middleware、healthz。
- `agent/runtime/api/runs.py`：request state 的使用。
- `agent/runtime/api/artifacts.py`：CAS + metadata。
- `agent/runtime/worker/main.py`：完全独立的执行装配。
- `common/sqlite_schema.py`：current-only schema 验证。
