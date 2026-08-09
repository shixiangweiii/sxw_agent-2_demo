# ADR-0005：Release 与 Schema Compatibility

- 状态：Accepted / Frozen
- 日期：2026-08-09

## Context

项目不迁移旧本地历史数据，但运行中的 durable Run 不能被新进程用不同 Prompt、Tool catalog 或 checkpoint schema 静默解释。

## Decision

### 1. 不可变 release manifest

Worker 启动时为三个 engine 分别注册 immutable manifest，并更新各 engine 的 active pointer。manifest 至少包含：

```text
runtime_schema_version
working_state_schema_version
event_schema_version
artifact_schema_version
evidence_schema_version
agent_release_id
engine
prompt_digest
model_policy_digest
tool_catalog_digest
skill_release_digest
retrieval_policy_digest
checkpoint_codec_digest
```

manifest 按字段排序、UTF-8、无多余空白的规范 JSON 计算 SHA-256 `release_fingerprint`。同一 fingerprint 的内容必须字节语义一致；manifest 不可 update/delete。

### 2. Admission freeze

CreateRun 必须找到请求 engine 的 active release，否则返回 `503 NO_ACTIVE_RELEASE`，不创建 Run。Admission 将 release fingerprint 写入 RuntimeEnvelope、Run、首 checkpoint/event；运行中 active pointer 变化不影响该 Run。唯一例外是下述显式 checkpoint upgrade：它不是 active pointer 自动覆盖，而是由精确注册的 upgrader 和 fenced/CAS 事务将该 Run 的**有效 release**从已知源版本切到已知目标版本。

### 3. Resume matrix

| Run/checkpoint 与 Worker | 结论 |
|---|---|
| fingerprint/schema 完全一致 | 继续执行 |
| schema/release 不同，但存在显式、版本对版本的已测试 checkpoint upgrader | 先原子追加升级后的 checkpoint，再继续；保留旧 checkpoint |
| 不同且无 upgrader | Coordinator 提交 `INCOMPATIBLE_RELEASE` terminal |
| DB migration 含未知新版本或 checksum 改写 | API/Worker 启动 fail-fast，不领取 Run |
| manifest fingerprint 相同但规范内容不同 | 启动 fail-fast，视为 release registry corruption |

禁止 silently best-effort 解析未知字段、把 active release 覆盖到已有 Run，或用新 Prompt/Tool catalog 重放旧 stable tool slot。

### 4. 显式 checkpoint upgrader 协议

upgrader 只能按以下完整 key 精确注册，不做通配、猜测、自动串链或传递闭包：

```text
(engine, from_release, from_schema, to_release, to_schema)
```

Coordinator 只把该 Run 的最新 committed checkpoint 交给 upgrader，且 checkpoint 的 `release_fingerprint/schema_version` 必须与 key 和 Run 当前有效 release 完全一致。没有 checkpoint、没有精确 key、转换异常或转换结果不符合目标 release 时，一律 fail closed 为 `INCOMPATIBLE_RELEASE`。

转换端口是同步纯函数：不得读写数据库/文件、调用模型/Tool/网络或依赖进程临时状态。转换发生在 SQLite 事务外。Store 发布转换结果时使用一个短 `BEGIN IMMEDIATE` 事务，并在同一事务内完成：

1. 校验当前 Activity lease/fencing token；
2. 用 checkpoint id + revision + source release/schema 重新验证它仍是最新 checkpoint；
3. 校验目标 release manifest 已注册、engine 一致、manifest schema 与目标 checkpoint schema 一致；
4. 追加新 revision 的 checkpoint，保留旧 checkpoint；
5. 将 Run 有效 release 切换为目标 release；
6. 追加带 source/target release、schema、checkpoint revision 的 `CHECKPOINT_COMMITTED` upgrade 审计事件。

任一步失败全部回滚。旧 fencing token 或 checkpoint revision CAS 冲突只表示当前 Worker 已失去写所有权，必须拒绝该 Worker 的升级；不能由 stale Worker 把 Run 裁决为 incompatible。

Admission 时返回的 RuntimeEnvelope 值对象仍是不可变快照；显式升级事务后，从 Store 重新读取的 Run envelope 会显示新的有效 release。除这一经过审计的字段切换外，admission envelope 字段不变；旧 checkpoint/event 继续保留原 release，从而完整记录解释边界。

### 5. 无历史迁移的准确含义

R4 可以停服并清理旧 `local_storage`，初始化新 runtime/rag schema，不迁移旧 Session、embedding 或 History。这不等于忽略新 Runtime 内已 accepted Run 的兼容性。要升级开发 schema，可以显式清空整个本地新库；只要库保留，就必须遵守 migration checksum 和 Run release freeze。

## Consequences

- 可重复演示和故障恢复拥有明确代码/Prompt/Tool 解释边界。
- 开发期不背旧格式兼容包袱，但不能静默误解释仍存在的 durable Run。
- Worker 启动注册 release 成为 API admission 前置条件。
- upgrader 是逐版本显式代码与可靠性测试资产；发布新 codec 时若不提供精确 upgrader，旧 active Run 会明确终止为 incompatible，而不会自动迁移。
