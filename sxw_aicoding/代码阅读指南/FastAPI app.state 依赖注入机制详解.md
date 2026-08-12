# FastAPI app.state 依赖注入机制详解

## 1. 当前结论

Runtime API 使用 FastAPI/Starlette 的 `app.state` 保存**本 API 进程内的组装完成对象**：

```text
app.state.settings
app.state.runtime_store
app.state.artifact_store
```

请求 handler 通过 `request.app.state` 取得它们，并将 Store/ArtifactStore 注入当次应用服务。

`app.state` 只是进程内对象容器，不是跨进程事实源。Run、release、Event、Checkpoint、Worker heartbeat 等权威仍在 `runtime.db`，Artifact bytes 在 SHA-256 CAS。

## 2. FastAPI lifespan 与 app.state

`agent/main.py` 通过 `@asynccontextmanager` 定义 lifespan：

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = RuntimeDatabase(
        settings.runtime_db_path,
        busy_timeout_ms=settings.runtime_busy_timeout_ms,
    )
    store = SqliteRuntimeStore(database)
    await store.initialize()

    app.state.settings = settings
    app.state.runtime_store = store
    app.state.artifact_store = FilesystemArtifactStore(settings.artifact_root)

    yield
```

`yield` 之前是启动阶段，只有它成功完成后 FastAPI 才开始接收请求。`yield` 之后是停机阶段；当前 RuntimeDatabase 的每次 read/transaction 都自行打开并关闭 SQLite connection，因此 lifespan 没有一条长连接需要 close。

### 2.1 为什么要在启动时 initialize

`store.initialize()` 进入 `RuntimeDatabase.ensure_schema()`，再调用 `common/sqlite_schema.py::ensure_current_schema()`。其语义只有两种成功结果：

1. DB 空：在单个 `BEGIN IMMEDIATE` 中建立完整 current schema，写 `schema_meta(id, schema_digest, created_at)`。
2. DB 非空：记录的 digest 与完整 `schema.sql` 原始字节 SHA-256 完全相等。

缺少 `schema_meta`、digest 不等或陌生 DB 都会以 `CURRENT_SCHEMA_MISMATCH` 启动失败，并要求操作者显式删除/重建本地 DB。API 不会在请求中悄悄修改库结构。

## 3. 三个注入对象的职责

### 3.1 settings

`AgentSettings` 是当前 API 进程的配置快照。HTTP 层当前使用的典型字段包括：

- Runtime DB path/busy timeout。
- Artifact root。
- Runtime 默认 deadline。
- ARAG 服务地址。
- API/trace 等配置。

虽然同一 `AgentSettings` 类型还定义模型和 Native 资源上限，API 进程不会因此加载 LLM；那些字段由独立 Worker 进程读取并纳入 release fingerprint。

### 3.2 runtime_store

`SqliteRuntimeStore` 实现 `RuntimeStore` port，为 API handler 提供：

- CreateRun admission/idempotency。
- Run/Activity 状态查询。
- committed Event replay/tail。
- cancel/signal。
- Artifact metadata 注册与查询。
- healthz 的 active release pointer 读取。

Store 对象本身可在同一 API 进程的并发请求间复用，但它不持有一个被全部请求共享的 transaction/connection。每个 Store 操作使用 RuntimeDatabase 上下文创建短连接，并在退出时关闭。

### 3.3 artifact_store

`FilesystemArtifactStore` 管理本机 SHA-256 CAS bytes。API 上传路径先完成 CAS 落盘，再通过 `runtime_store.register_artifact_metadata()` 写入持久化 metadata。

因此 Artifact 路径同时使用两个注入对象：

```text
request.app.state.artifact_store  -> 写/读 CAS bytes
request.app.state.runtime_store   -> metadata 和引用权威
```

## 4. 请求阶段怎么取依赖

### 4.1 Run API

`agent/runtime/api/runs.py` 用一个窄 helper 隔离 Store 获取：

```python
def _store(request: Request) -> RuntimeStore:
    return request.app.state.runtime_store
```

CreateRun 每次请求构造轻量的 `AdmissionService`：

```python
settings: AgentSettings = request.app.state.settings
service = AdmissionService(
    _store(request),
    default_deadline_ms=(
        settings.runtime_default_deadline_seconds * 1000
    ),
)
```

`AdmissionService` 依赖 `RuntimeStore` protocol，而不是在应用层新建 SQLite connection。这使应用规则与持久化细节分离，测试也可注入 fake store/clock。

### 4.2 Artifact API

`agent/runtime/api/artifacts.py` 直接取 ArtifactStore 和 RuntimeStore。读 Artifact 前先查权威 metadata，再按 digest id 读 CAS，并校验单 Range 最大读取边界。

### 4.3 Documents API

`agent/api/documents.py` 从 `request.app.state.settings` 获取 ARAG 连接配置。Runtime API 在这里是对 ARAG HTTP 端点的轻量接入，不会把 ARAG 索引对象注入本进程。

### 4.4 healthz

`GET /healthz` 通过 `runtime_store.active_releases()` 返回库中的三个 active pointer。这只能证明 API 可读 DB，不能单独证明当前 Worker ready。

完整 Worker readiness 还必须检查本次启动后的新鲜 `ACTIVE` heartbeat，并且 heartbeat 中的三引擎 `release_map` 与 active pointers 完全一致。

## 5. app.state 与 Worker 装配是两条独立路径

这是阅读代码时最容易混淆的点。

```text
FastAPI API process
  lifespan
  -> settings + runtime_store + artifact_store
  -> app.state
  -> HTTP handlers

Runtime Worker process
  build_worker()
  -> settings + runtime_store + artifact_store
  -> LLM + Skill/A2A/Claude Skill 目录
  -> strict ToolCatalog + mandatory Tool Broker
  -> 3 ReleaseManifest
  -> 2 AdkEngineAdapter + 1 NativeLoopAdapter
  -> activate_current_releases(三指针原子切换)
  -> RuntimeWorker
```

Worker 不是 FastAPI app 的后台 task，也不读 `app.state`。两个进程通过共享 `runtime.db` 和 Artifact CAS 交接，进程内对象不跨边界。

### 5.1 Release 为什么不放进 API app.state

release 依赖 Worker 实际加载的 ToolCatalog、Engine/runtime/shared source、provider/model、checkpoint codec、语义配置、资源上限和安装依赖版本。API 进程不加载这些能力，所以它不能自己计算/激活 release。

Worker 只在三个 Adapter 都构造成功后，调用 `activate_current_releases()` 一次性写入/核对 immutable manifests，检查旧 fingerprint 非终态 Run，原子切换三个 pointer。

## 6. 三引擎也不在 app.state 中

RunCoordinator 统一调用：

```text
EngineAdapter.execute(EngineRunRequest, RuntimeIO) -> EngineOutcome
```

Worker 中的 EngineRegistry 存放：

```text
plan_execute -> AdkEngineAdapter
agent_loop   -> AdkEngineAdapter
native_loop  -> NativeLoopAdapter
```

Native Adapter 直接负责 strict checkpoint、RuntimeIO event/checkpoint、mandatory Broker 和 final assistant，不经过 ADK merge queue。两个 ADK Adapter 每 attempt 创建临时 session/artifact service。

这些都是 Worker 内对象，与 HTTP request 的 `request.app.state` 没有引用关系。

## 7. 依赖注入与事实权威要分开

| 概念 | 是什么 | 不是什么 |
|---|---|---|
| `app.state.runtime_store` | Store adapter 对象引用 | Run 状态的内存副本 |
| `app.state.artifact_store` | CAS 文件 adapter | Artifact metadata authority |
| `app.state.settings` | API 进程配置快照 | 已激活 release |
| `runtime.db` | admission/run/event/checkpoint/release authority | 只服务于某一 HTTP worker 的 session |
| `runtime_workers.release_map_json` | Worker 能力/心跳事实 | API app.state 里的 EngineRegistry |

例如，同一 API 进程重建一个 `SqliteRuntimeStore` 对象不会丢 Run；删掉/替换 `runtime.db` 才会改变权威数据。反过来，即使 `app.state` 中还有 Store 对象，schema digest 不匹配时也不能继续使用该 DB。

## 8. 测试方式

### 8.1 走完整 FastAPI lifespan

集成测试应使用会触发 lifespan 的 ASGI/TestClient 方式，这样能验证：

- current schema bootstrap/identity 检查。
- `app.state` 三个对象已在请求前注入。
- handler 走真实 Store/ArtifactStore 路径。

### 8.2 直接注入测试替身

单元测试可创建 FastAPI app/request 并显式设置：

```python
app.state.settings = test_settings
app.state.runtime_store = fake_store
app.state.artifact_store = fake_artifact_store
```

但 fake 不能改变业务契约。例如 claim 仍应接收完整 `release_map`，checkpoint 仍应使用 revision CAS，所有权丢失仍应以 `AttemptOwnershipLost` 冒泡而不是 terminalize Run。

## 9. 常见问题

### 为什么不把 AdmissionService 也做成 app.state 单例？

它当前只持有 Store、Clock 和默认 deadline，每请求构造成本很低，而且让 handler 明确地把当前 settings 中的 deadline 注入进去。没有必要为了形式一致增加一个应用级对象。

### app.state 是否线程/协程安全？

`app.state` 只提供对象引用，安全性由对象本身决定。当前 Store 不共享可变 transaction connection，SQLite 写通过短 `BEGIN IMMEDIATE` 串行化；Artifact CAS 使用 digest 和原子 rename。不能因为对象被放在 `app.state` 就推导出内部实现自动并发安全。

### 多 API 进程的 app.state 是同一个吗？

不是。每个进程有各自的 app/state/Store adapter 对象，它们通过共享的 SQLite 文件交流。当前这一设计边界是本机多进程，不是跨主机 HA。

### 可以在 API lifespan 里创建 Worker task 吗？

不应该。这会让 API 进程加载 LLM/工具目录，破坏独立部署、故障隔离、新鲜 heartbeat 和 release activation 语义。执行层必须保持为 `python -m agent.runtime.worker.main` 独立进程。

## 10. 源码阅读索引

- `agent/main.py`：FastAPI lifespan、app.state、healthz。
- `agent/runtime/api/runs.py`：Store/settings 获取、CreateRun、status/cancel/signal/SSE。
- `agent/runtime/api/artifacts.py`：ArtifactStore + RuntimeStore 组合使用。
- `agent/api/documents.py`：settings 驱动的 ARAG HTTP 接入。
- `agent/runtime/application/admission.py`：依赖 `RuntimeStore` 的应用服务。
- `agent/runtime/adapters/sqlite/database.py`：每操作 connection 策略与 current schema 入口。
- `common/sqlite_schema.py`：schema byte digest 和原子 bootstrap。
- `agent/runtime/worker/main.py`：与 app.state 独立的 Worker/ToolCatalog/release/Adapter 装配。
