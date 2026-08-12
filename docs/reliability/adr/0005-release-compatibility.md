# ADR-0005：Current Schema 与 Exact Release Claim

- 状态：Accepted / Frozen
- 日期：2026-08-12
- 替代：2026-08-09 版的 release 不匹配终态语义

## Context

项目不兼容旧本地数据、旧 checkpoint codec 或滚动发布期间的跨版本恢复；换 schema 时显式删库重建。但是只要一个 durable Run 已经 admission，它就必须由创建时精确的代码、Prompt、Tool catalog、provider policy 和 checkpoint codec 解释，不得被新 Worker 猜测执行。

## Decision

### 1. 单一 current schema identity

Runtime 和 ARAG 各只有一份 current `schema.sql`。`schema_meta` 仅包含：

```text
id = 1
schema_digest = SHA-256(完整 schema.sql 原始字节)
created_at
```

空库在一个 `BEGIN IMMEDIATE` 中创建全部表并写 identity。非空库只接受完全相同的 digest；否则 `SchemaIdentityError(code="CURRENT_SCHEMA_MISMATCH")` 导致 API/Worker/ARAG 启动 fail-fast，并提示使用者显式删除或重建对应本地库。

不存在 migration、`ALTER` 路径、upgrader、双读、shadow schema 或旧 checkpoint codec。程序不得自动删库或改写旧库。

### 2. 不可变 release manifest

`ReleaseManifest` 只包含 `engine + components`。components 必须覆盖：

- current Runtime/ARAG schema digest；
- engine、Runtime 与共享源码 digest；
- 经过 strict 校验的最终 ToolCatalog digest；
- 模型、provider 协议与唯一 checkpoint codec；
- 全部语义配置、工具提前派发 mode 和资源硬上限；
- 真实已安装依赖版本。

manifest 按字段排序、UTF-8、无多余空白的规范 JSON 计算 SHA-256 `release_fingerprint`。同 fingerprint 的内容必须规范字节相同；manifest 不可 update/delete。`requirements.txt` 精确锁定并逐项登记项目直接运行依赖，manifest 写入这些 distribution 的真实安装版本；不采集带平台差异和无关工具的全环境 `pip freeze`。任一必需依赖 metadata 缺失时 Worker 启动失败，不填 `unknown` 或 requirements 中的假定版本。

### 3. 三 release 原子激活

Worker 在一个 `BEGIN IMMEDIATE` 中完成：

1. 写入或核对三份 immutable manifest；
2. 确认不存在与目标 fingerprint 不同的非终态 Run；
3. 原子切换三个 active pointer。

同 fingerprint 的多 Worker 可并存。新 fingerprint 遇到活跃旧 Run 时整体失败 `ACTIVE_RUNS_BLOCK_RELEASE_ACTIVATION`，不发布半套 pointer。Admission 和 activation 都在写事务中读写 active pointer，因此不存在“用旧 pointer 接收新 Run，同时切到新 release”的空窗。

### 4. Admission freeze 与 exact claim

CreateRun 必须找到请求 engine 的 active release，否则返回 `503 NO_ACTIVE_RELEASE` 且不创建 Run。Admission 把 fingerprint 写入 RuntimeEnvelope/Run，之后不可改写；active pointer 变化不影响已创建 Run。

Worker 的普通、恢复和 reconcile claim 全部使用完整 `release_map`，SQL 同时匹配 `(engine, release_fingerprint)`。不匹配的 Worker 根本无法领取 Activity；Run 保持 pending，直到匹配 Worker 恢复或绝对 deadline 把它收口。release 不匹配不是 Run terminal 状态。

Coordinator 仍保留理论上不可达的 `CLAIM_RELEASE_MISMATCH` 防御断言：若 Store/Worker 契约被破坏，只中止当前 attempt 并报警，不产生 Run 终态。

## Consequences

- fresh-Run 恢复的代码/Prompt/Tool/provider/checkpoint 解释边界可验证；
- 发布策略是“先让旧 Run 结束，再原子激活新 release”，不是滚动兼容；
- 开发期换 schema/release 需要停服并显式重建本地数据，这是 current-only 的预期运维动作；
- 不通过一个“不兼容”终态把发布/调度配置错误伪装成业务运行结果。
