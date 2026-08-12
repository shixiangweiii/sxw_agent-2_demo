# ADR-0006：权威对象与公开投影的序列化边界

状态：**ACCEPTED**  
日期：2026-08-09

## 背景

R0 最初六份 JSON Schema 混入了尚未实现的字段名和另一套表示法：UUID 被写成带连字符形式，Runtime Store 的 epoch-millisecond 时间被写成 RFC3339，`ArtifactRef` 被混入 SQLite link/retention 字段，`ToolResultEnvelope` 被混入 `tool_executions` 账本字段，Evidence 则使用了与真实 ARAG/Broker 产物不同的字段名。结果是 Schema 本身合法，但不能验证任何真实权威对象，也无法在门禁中阻止领域模型漂移。

## 决策

1. 六份 v1 Schema 必须验证真实权威对象的 `model_dump(mode="json")` 或 strict 边界 adapter 产物，而不是描述一个并不存在的汇总 DTO。
2. Runtime 业务 ID 使用 `<type>_` + `uuid.UUID.hex` 的 32 位小写 compact UUID。业务根对象为 UUIDv4；Activity/ToolExecution 等稳定子身份为 UUIDv5。无连字符只是等价编码，不改变 UUID 版本或身份语义。
3. `RuntimeEnvelope`、`CanonicalEvent` 和 checkpoint 内 `WorkingState` 属于 Runtime Store/领域边界，绝对时间使用 UTC epoch milliseconds。公开 GET/SSE 在 API 边界把时间转为 RFC3339 UTC；公开投影不是 Canonical Event authority。
4. `ArtifactRef` 精确表示 CAS 返回值和上传 API 响应。storage path、来源 link、sensitivity 与 retention 只存在于 `artifact_metadata/artifact_links` 权威表，不复制进 Ref。
5. `ToolResultEnvelope` 只表示 `SUCCESS/FAILURE/INTERRUPT/NO_OUTPUT/UNKNOWN` 及有界结果语义。稳定 identity、attempt、effect state、reconcile 与时间只由 `tool_executions` 账本裁决。
6. 工具协议边界必须在进入 Broker 前生成 strict `ToolExecutionOutput(result, evidence)`。Evidence producer（例如 `knowledge_search`）负责填全 principal、dataset/scope/query、RetrievalStatus/degraded reasons，以及每条 Evidence 的 document/index version、content hash、page/span、scope/query identity；Broker 只校验 run/activity/tool execution 身份并写 Artifact，不补造 provenance、不转换 legacy hits、不接受隐藏 evidence 字段。模型仍只接收有界 hits preview。
7. `tests/reliability/test_schema_contracts.py` 使用 Draft 2020-12、format checker 和真实对象验证六份 Schema。`scripts/check.sh` 执行完整 reliability suite，因此领域字段或表示法无版本漂移会直接失败。

## 结果

- SQLite 的持久形状、Artifact JSON 与 SSE/API 投影不再共享一个模糊时间约定。
- Schema 的 `additionalProperties: false` 能检测真实模型新增字段；版本演进必须先更新 ADR/Schema/测试。
- Artifact/Evidence 的完整权威信息仍各归其唯一所有者，不会为了一个“万能 envelope”复制 SQLite ledger 字段。
- 本修订不引入旧协议兼容层，也不改变服务端 Delivery v1 承诺。

## 被替代的表述

本 ADR 替代六份 v1 Schema 中与实际 authority object 冲突的原字段列表、带连字符 UUID pattern 和“一律 RFC3339”的含混描述；不替代 ADR-0001 至 ADR-0005 的事务、提交、SQLite、恢复或 release 语义。
