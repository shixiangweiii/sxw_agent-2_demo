# FastAPI app.state 依赖注入机制详解

## 一、概述

本项目使用 FastAPI 的 `app.state` 实现依赖注入，而非常见的 `Depends()` 机制。本文档详细解释这一设计决策的实现原理、调用链路和最佳实践。

### 1.1 核心概念

**`app.state`** 是 FastAPI/Starlette 提供的应用级状态容器：
- 生命周期与应用绑定，应用启动时创建，停止时销毁
- 可在 lifespan 中初始化单例资源（数据库连接、配置等）
- 通过 `request.app.state` 在请求处理中访问
- 本质是一个命名空间对象，可动态添加属性

**依赖注入模式**：
- 控制反转（IoC）：对象的创建和管理权交给容器
- 依赖抽象而非具体实现：使用 Protocol 定义接口
- 单例模式：应用级资源共享

---

## 二、项目实现详解

### 2.1 启动阶段：资源初始化与注入

**文件**：`agent/main.py:43-59`

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # ① 创建数据库连接管理器
    database = RuntimeDatabase(
        settings.runtime_db_path,
        busy_timeout_ms=settings.runtime_busy_timeout_ms,
    )
    
    # ② 创建 Store 实例（依赖注入的核心对象）
    store = SqliteRuntimeStore(database)
    await store.initialize()  # 校验/建库当前 schema（无 migration，内部调用 database.ensure_schema()）
    
    # ③ 注入到 app.state（IoC 容器）
    app.state.settings = settings
    app.state.runtime_store = store
    app.state.artifact_store = FilesystemArtifactStore(settings.artifact_root)
    
    log_kv(logger, logging.INFO, "Boot", "runtime API starting", ...)
    yield  # 应用运行期间
    
    # ④ 应用停止时的清理逻辑
    log_kv(logger, logging.INFO, "Boot", "runtime API stopped")
```

**关键点**：
- `lifespan` 是 FastAPI 的异步上下文管理器，应用启动时执行 `yield` 之前，停止时执行之后
- 所有单例资源在此初始化，保证全局唯一
- `app.state` 作为 IoC 容器，存储所有共享资源

---

### 2.2 请求阶段：获取依赖

**文件**：`agent/runtime/api/runs.py:80-81`

```python
def _store(request: Request) -> RuntimeStore:
    """从 app.state 获取 Store 实例"""
    return request.app.state.runtime_store
```

**使用场景**：`agent/runtime/api/runs.py:131-176`

```python
@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunBody,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    # ① 从 app.state 获取 store
    store = _store(request)
    
    # ② 从 app.state 获取 settings
    settings: AgentSettings = request.app.state.settings
    
    # ③ 创建业务服务，注入依赖
    service = AdmissionService(
        store,  # 注入 Store
        default_deadline_ms=settings.runtime_default_deadline_seconds * 1000,
    )
    
    # ④ 调用业务逻辑
    result = await service.create(
        CreateRunInput(...),
        idempotency_key=idempotency_key or "",
    )
```

---

### 2.3 业务逻辑层：依赖传递

**文件**：`agent/runtime/application/admission.py:38-83`

```python
class AdmissionService:
    def __init__(
        self,
        store: RuntimeStore,  # 依赖抽象接口（Protocol）
        *,
        clock: Clock | None = None,
        default_deadline_ms: int = 600_000,
    ) -> None:
        self.store = store
        self.clock = clock or SystemClock()
        self.default_deadline_ms = default_deadline_ms
    
    async def create(self, request: CreateRunInput, *, idempotency_key: str) -> AdmissionResult:
        # 业务逻辑...
        return await self.store.admit(command)  # 调用依赖的方法
```

**设计原则**：
- **依赖倒置**：`AdmissionService` 依赖 `RuntimeStore` 协议，而非 `SqliteRuntimeStore` 具体实现
- **构造器注入**：通过 `__init__` 参数注入依赖
- **可测试性**：测试时可注入 Mock 实现

---

### 2.4 数据访问层：具体实现

**文件**：`agent/runtime/adapters/sqlite/store.py:324+`

```python
class SqliteRuntimeStore:
    """RuntimeStore 协议的 SQLite 实现"""
    
    def __init__(self, database: RuntimeDatabase) -> None:
        self.db = database
    
    async def admit(self, command: AdmissionCommand) -> AdmissionResult:
        # 具体的 SQLite 操作
        async with self.db.transaction() as conn:
            # INSERT/UPDATE/SELECT ...
            pass
```

---

## 三、完整调用链路图

```
┌─────────────────────────────────────────────────────────────────┐
│ 应用启动阶段 (lifespan)                                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ① RuntimeDatabase(path)                                        │
│      ↓                                                          │
│  ② SqliteRuntimeStore(database)                                 │
│      ↓                                                          │
│  ③ app.state.runtime_store = store  ─────────────┐             │
│      ↓                                           │             │
│  ④ app.state.settings = settings                 │             │
│      ↓                                           │             │
│  ⑤ app.state.artifact_store = artifact_store     │             │
│                                                  │             │
└─────────────────────────────────────────────────────────────────┘
                                                  │
┌─────────────────────────────────────────────────────────────────┐
│ HTTP 请求处理阶段                                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  POST /api/v1/runs                                              │
│      ↓                                                          │
│  create_run(body, request, ...)                                 │
│      ↓                                                          │
│  store = _store(request)  ←─────────────────────────┘          │
│           │                                                     │
│           ↓                                                     │
│  request.app.state.runtime_store                                │
│      ↓                                                          │
│  service = AdmissionService(store, ...)                         │
│      ↓                                                          │
│  result = service.create(request)                               │
│      ↓                                                          │
│  self.store.admit(command)                                      │
│      ↓                                                          │
│  SqliteRuntimeStore.admit()                                     │
│      ↓                                                          │
│  database.transaction()                                         │
│      ↓                                                          │
│  SQLite 执行 SQL                                                │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 四、app.state 中注入的资源清单

| 资源名称 | 类型 | 注入位置 | 用途 |
|---|---|---|---|
| `settings` | `AgentSettings` | `agent/main.py:51` | 全局配置（LLM、超时、日志等） |
| `runtime_store` | `SqliteRuntimeStore` | `agent/main.py:52` | Runtime 状态存储（Run/Event/Activity） |
| `artifact_store` | `FilesystemArtifactStore` | `agent/main.py:53` | Artifact 文件存储 |
| `ctx` | `AragContext` | `arag/main.py:27` | ARAG 检索上下文（仅 ARAG 服务） |

---

## 五、为什么选择 app.state 而非 Depends

### 5.1 FastAPI Depends 机制简介

FastAPI 官方推荐的依赖注入方式是 `Depends()`：

```python
from fastapi import Depends

async def get_store(request: Request) -> SqliteRuntimeStore:
    return request.app.state.runtime_store

@router.post("")
async def create_run(
    body: CreateRunBody,
    store: SqliteRuntimeStore = Depends(get_store),
):
    # 使用 store
```

### 5.2 本项目的选择理由

| 维度 | app.state | Depends | 本项目选择 |
|---|---|---|---|
| **显式性** | 需手动从 `request.app.state` 取 | 函数签名声明，IDE 友好 | app.state |
| **灵活性** | 可在任意位置访问 | 仅限路由函数参数 | app.state |
| **测试** | 需 Mock `app.state` | 可直接传参 | Depends 略优 |
| **性能** | 无额外开销 | 每次请求调用依赖函数 | app.state 略优 |
| **学习成本** | 简单直接 | 需理解依赖解析 | app.state |
| **代码风格** | 命令式，显式调用 | 声明式，隐式注入 | app.state 更统一 |

**核心决策因素**：
1. **统一性**：项目有多个服务（Agent API、Worker、ARAG、SkillCenter、A2A），统一使用 `app.state` 降低心智负担
2. **显式控制**：依赖获取位置明确，便于调试和追踪
3. **无类型约束**：部分依赖是动态类型（如 `SimpleNamespace` 配置的测试），`Depends` 的类型推断反而成为限制
4. **历史原因**：项目早期采用此模式，已形成惯例

### 5.3 两种模式对比示例

**app.state 模式（本项目）**：
```python
@router.post("")
async def create_run(request: Request, body: CreateRunBody):
    store = request.app.state.runtime_store      # 显式获取
    settings = request.app.state.settings         # 显式获取
    service = AdmissionService(store, ...)
    return await service.create(...)
```

**Depends 模式（假设）**：
```python
def get_store(request: Request) -> SqliteRuntimeStore:
    return request.app.state.runtime_store

def get_settings(request: Request) -> AgentSettings:
    return request.app.state.settings

@router.post("")
async def create_run(
    body: CreateRunBody,
    store: SqliteRuntimeStore = Depends(get_store),      # 声明式注入
    settings: AgentSettings = Depends(get_settings),     # 声明式注入
):
    service = AdmissionService(store, ...)
    return await service.create(...)
```

**结论**：本项目依赖较少（3-5 个核心资源），`app.state` 模式足够清晰，无需引入 `Depends` 的复杂性。

---

## 六、最佳实践

### 6.1 资源初始化原则

1. **单例模式**：所有 `app.state` 资源在 `lifespan` 中初始化，保证全局唯一
2. **异步初始化**：使用 `await store.initialize()` 执行耗时操作（如 schema 建库/身份校验，见 `common/sqlite_schema.py` 的 `ensure_current_schema`）
3. **资源清理**：`yield` 之后执行清理逻辑（如关闭连接、刷新缓存）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化
    resource = await create_resource()
    app.state.resource = resource
    yield
    # 清理
    await resource.close()
```

---

### 6.2 依赖获取封装

将 `app.state` 访问封装为辅助函数，避免重复代码：

```python
def _store(request: Request) -> RuntimeStore:
    return request.app.state.runtime_store

def _settings(request: Request) -> AgentSettings:
    return request.app.state.settings

def _artifact_store(request: Request) -> ArtifactStore:
    return request.app.state.artifact_store
```

**优点**：
- 减少拼写错误
- 便于统一修改
- 类型提示友好

---

### 6.3 测试中的依赖注入

测试时手工构造 `app.state`（不需要 Mock Store，可靠性测试直接用 `tmp_path` 起一个真实 SQLite 库）：

```python
# tests/reliability/test_runtime_api.py:36-62（_build_api）+ 65-79（api_env fixture）
def _build_api(store: SqliteRuntimeStore, artifacts: FilesystemArtifactStore, *, ...) -> FastAPI:
    app = FastAPI()
    app.state.runtime_store = store
    app.state.artifact_store = artifacts
    app.state.settings = SimpleNamespace(...)  # 模拟配置对象
    app.include_router(run_router)
    return app

@pytest.fixture
async def api_env(tmp_path):
    store = SqliteRuntimeStore(RuntimeDatabase(tmp_path / "runtime.db"))
    await store.initialize()
    ...
    app = _build_api(store, artifacts)
    ...
```

**关键点**：
- 用真实 `SqliteRuntimeStore` + `tmp_path` 临时库，而非 Mock/Fake：可靠性测试要验证真实 SQLite 事务/约束行为，Mock 会掩盖这一层
- `SimpleNamespace` 只模拟配置对象（`settings`），不模拟 Store
- `tmp_path` 保证测试隔离，不污染 `local_storage/`

---

### 6.4 类型安全

使用 Protocol 定义接口，保证类型安全：

```python
# agent/runtime/ports/store.py
class RuntimeStore(Protocol):
    async def admit(self, command: AdmissionCommand) -> AdmissionResult: ...
    async def get_run(self, run_id: str) -> RunRecord | None: ...
    # ...

# agent/runtime/adapters/sqlite/store.py
class SqliteRuntimeStore:
    async def admit(self, command: AdmissionCommand) -> AdmissionResult:
        # 实现 Protocol 定义的方法
        pass
```

**优点**：
- 编译时类型检查
- IDE 自动补全
- 替换实现时保证接口一致

---

## 七、常见问题与陷阱

### 7.1 AttributeError: 'State' object has no attribute

**原因**：访问未初始化的 `app.state` 属性

**解决**：
- 检查资源是否在 `lifespan` 中初始化
- 确保路由挂载在 `lifespan` 之后
- 测试时手动设置 `app.state`

---

### 7.2 循环依赖

**问题**：
```python
class ServiceA:
    def __init__(self, b: ServiceB): ...

class ServiceB:
    def __init__(self, a: ServiceA): ...
```

**解决**：
- 重构为单向依赖
- 使用延迟初始化（lazy initialization）
- 引入中间层解耦

---

### 7.3 资源泄漏

**问题**：`yield` 之后未清理资源

**解决**：
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    resource = await create_resource()
    app.state.resource = resource
    try:
        yield
    finally:
        await resource.close()  # 确保清理
```

---

### 7.4 并发访问

**问题**：`app.state` 在并发请求下是否安全？

**答案**：安全。`app.state` 是只读访问（启动时写入，运行时只读），无需加锁。但注意：
- 资源本身需要线程安全（如数据库连接池）
- 不要在请求处理中修改 `app.state`

---

## 八、总结

本项目基于 FastAPI `app.state` 实现了简洁有效的依赖注入：

**核心优势**：
1. **简单直接**：无需学习复杂的依赖解析机制
2. **显式控制**：依赖获取位置明确，便于调试
3. **统一风格**：所有服务使用相同模式
4. **性能优越**：无额外函数调用开销

**适用场景**：
- 依赖数量较少（<10 个核心资源）
- 团队偏好显式代码
- 项目规模中等，无需复杂的依赖图管理

**不适用场景**：
- 依赖关系复杂，需要自动解析
- 需要细粒度的作用域控制（per-request、per-session）
- 团队熟悉并偏好声明式风格

**替代方案**：
- FastAPI `Depends`：适合依赖复杂、需要类型推断的场景
- 第三方 IoC 容器（如 `dependency-injector`）：适合大型项目、复杂依赖图
