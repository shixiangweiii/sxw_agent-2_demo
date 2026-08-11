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

CreateRun 必须找到请求 engine 的 active release，否则返回 `503 NO_ACTIVE_RELEASE`，不创建 Run。Admission 将 release fingerprint 写入 RuntimeEnvelope、Run、首 checkpoint/event。`runs.release_fingerprint` 自 admission 起不可变，与 `engine` 同级冻结：运行中 active pointer 变化不影响该 Run，也没有任何代码路径改写它。

### 3. Resume matrix

| Run/checkpoint 与 Worker | 结论 |
|---|---|
| fingerprint/schema 完全一致 | 继续执行 |
| 任何不一致 | Coordinator 提交 `INCOMPATIBLE_RELEASE` terminal |
| DB schema identity 不符（version/checksum 不匹配或缺失 `schema_meta`） | API/Worker 启动 fail-fast，不领取 Run |
| manifest fingerprint 相同但规范内容不同 | 启动 fail-fast，视为 release registry corruption |

禁止 silently best-effort 解析未知字段、把 active release 覆盖到已有 Run，或用新 Prompt/Tool catalog 重放旧 stable tool slot。

### 4. 不提供 checkpoint 升级路径

数据库跨版本升级和滚动发布期间的 schema 兼容不在本项目范围内，因此**不存在** checkpoint upgrader：没有升级注册表、没有转换端口，也没有切换 Run 有效 release 的事务。release 不一致时 Coordinator 只有一个结论，即 `INCOMPATIBLE_RELEASE`。

这个判定不读 checkpoint，因此发生在加载 checkpoint 之前。它仍受 Activity lease/fencing token 保护：陈旧 Worker 无法把 Run 裁决为终态。

Admission 时返回的 RuntimeEnvelope 值对象是不可变快照，且没有任何代码路径改写 `runs.release_fingerprint`；旧 checkpoint/event 永远保留其原始 release，完整记录解释边界。

### 5. 无历史迁移的准确含义

可以随时停服、删除整个 `local_storage`、用当前 schema 重新初始化 runtime/rag 库，不迁移任何旧数据。这不等于忽略新 Runtime 内已 accepted Run 的兼容性：只要库保留，就必须遵守 schema identity 校验和 Run release freeze。要换 schema，就删库重建，程序不会替你迁移。

## Consequences

- 可重复演示和故障恢复拥有明确代码/Prompt/Tool 解释边界。
- 开发期不背旧格式兼容包袱，但不能静默误解释仍存在的 durable Run。
- Worker 启动注册 release 成为 API admission 前置条件。
- 发布新 codec 时，旧 active Run 会明确终止为 `INCOMPATIBLE_RELEASE`，不会被自动迁移或猜测解释。开发期要避免这个终态，就在没有未终态 Run 时再升级代码。
