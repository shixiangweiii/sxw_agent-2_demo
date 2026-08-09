# ADR-0007：跨进程诊断 trace 关联键

状态：**ACCEPTED**  
日期：2026-08-09

## 背景

把执行从 API 进程搬到独立 Runtime Worker 之后，`common/trace.py` 的 span 树本身完好，但关联键断了。

`trace_id` 原先只由 `TraceMiddleware` 写进 contextvar，执行也发生在同一个请求上下文里。执行搬走后，Worker 进程既没有 HTTP 中间件，也没有任何通道能拿到入口观察到的 `x-trace-id`——contextvar 跨不过 API → SQLite → Worker 这道进程边界。于是 Worker 侧 `get_trace_id()` 恒返回默认值 `-`，实际后果有三个：

1. 一个 Worker 进程生命周期内**所有 Run 的 span 塌进同一条 trace**，落进同一个 JSONL 文件，互相污染；
2. `GET /api/v1/traces/{trace_id}` 对任何 Run 都 404，`eval/harness` 因此把每条 case 标成 `no_trace`，失败归因整体失效；
3. 内存 ring buffer 按 trace_id 淘汰，只有 `-` 一个键时永不淘汰，长驻 Worker 上表现为无界增长。

原 REL-029 用例没能拦住这个回归，因为它自己调用了 `set_trace_id()`，恰好补上了生产链路缺失的那一步。

## 决策

1. `runs` 新增 `trace_id` 列（migration `003_run_trace_id.sql`，`NOT NULL DEFAULT ''`），承载 admission 时观察到的 `x-trace-id`。
2. 该字段**不进入 `RuntimeEnvelope`**。`runtime-envelope-v1` 是 R0 冻结的入口身份契约，而 trace 只诊断；诊断字段挂在 `RunRecord` 上，与 `status`/`input_text` 同层，冻结的六份 Schema 一字不改。
3. 该字段**不参与 `request_digest`**。否则同一请求换个 `trace_id` 重放会被误判成 409 digest 冲突，破坏幂等语义。
4. `RunCoordinator.execute_claim` 在执行前用 `common.obs.use_trace_id` 绑定 `run.trace_id`，缺失时回落 `run_id`，保证每个 Run 始终有唯一且可查的轨迹键。
5. 绑定必须**按 token 还原**而不是裸 `set_trace_id`。Worker 的 `run()` 给每个 claim 起独立 task（context 是副本），但 `run_once()` 是直接 await 的确定性入口，裸 set 会泄漏给调用方并在连续调用间串味。

## 结果

- 一个 Run 一条 trace、一个文件；`GET /api/v1/traces/{trace_id}` 按客户端给的 id 可取回，eval harness 的联查键恢复。
- 未带 `x-trace-id` 的 Run 以 `run_id` 为键，仍然可查，不再共用 `-`。
- ring buffer 恢复按 Run 淘汰的原意，无界增长消失。
- Trace 的 Diagnostic 定位不变：`runs.trace_id` 只是写在权威表里的关联键，不参与任何 Run/Activity/Tool/terminal 裁决，关闭 tracing 也不影响恢复（REL-029 的既有断言继续成立）。

## 被替代的表述

本 ADR 替代"trace_id 由请求上下文隐式贯穿全链路"这一在单进程时代成立、在四服务五进程下不再成立的假设。不替代 ADR-0001 至 ADR-0006 的事务、提交、SQLite、恢复、release 或序列化语义；不改变 `runtime-envelope-v1` 及其余五份冻结 Schema。
