# 移除 schema migration 机制与 checkpoint upgrader

- 日期：2026-08-12
- 范围：Runtime SQLite、ARAG SQLite、Coordinator release 判定、门禁脚本、reliability 文档
- 规模：28 个跟踪文件 `+111 / -1384`，新增 4 个文件共 550 行；净减约 830 行

---

## 一、背景

### 1.1 需求来源

`sxw_aicoding/项目背景说明.txt`（更新于 2026/8/10）第 7–10 行提出的硬约束：

> 保持简洁，不需要兼容旧 API、旧行为和旧业务数据，也不要保留 schema migration 机制，也不要为了旧数据保留兼容层；
> 生产级要求覆盖运行时正确性、多实例协作和故障恢复，但数据库跨版本升级、滚动发布期间的 schema 兼容和旧数据保留不在本项目范围内；
> 数据库只支持当前 schema。schema 不匹配时必须 fail-fast，并提示用户显式删除或重建对应本地数据库；程序不得静默删除、覆盖或自动迁移旧数据库。

该项目由公司生产链路抽取简化，用于个人学习与面试准备，不承担线上流量，因此没有任何"必须读旧库"的真实约束。用户在本次改动前已手工清空 `local_storage/`，明确表示不关心历史数据。

### 1.2 改动前的三套违背该约束的机制

| # | 机制 | 位置 | 性质 |
|---|---|---|---|
| 1 | Runtime 增量 migration | `agent/runtime/adapters/sqlite/migrations/001,002,003` + `schema_migrations` 表 + `RuntimeDatabase.migrate()` | 旧库兼容 |
| 2 | ARAG 增量 migration | `arag/persistence/migrations.py` 的 `MIGRATIONS` 元组 + `LATEST_SCHEMA_VERSION` + `RagRepository.initialize()` 应用循环 | 旧库兼容 |
| 3 | Checkpoint 跨 release upgrader | `ports/application/release_compatibility.py`、`store.publish_checkpoint_upgrade`、`coordinator` 分支 4 | 滚动发布期 schema 兼容 |

### 1.3 评审阶段发现的四个实质问题

1. **002/003 是纯粹的旧库兼容增量。**`002` 给 `tool_executions` 加 `supports_reconcile`、`003` 给 `runs` 加 `trace_id`，两列在插入时都显式赋值（`store.py` 的 `INSERT INTO runs` 与 `INSERT INTO tool_executions`），`NOT NULL DEFAULT` 只是为了让 `ALTER TABLE ADD COLUMN` 能作用在旧库上。

2. **`verify()` 名不副实：它会先迁移再验证。**`database.py` 的 `verify()` 第一句就是 `await self.migrate()`；`check.sh` 连续调用 `migrate(); verify(); verify()` 并注释成"第二遍验证已记录的 checksum"。对一个真实的落后库，这条路径是**先改写它、再宣布验证通过**，与"程序不得静默迁移旧数据库"直接冲突。当时没暴露，只是因为 `check.sh` 用的是 `mktemp` 空目录。

3. **fail-fast 没接到生产启动路径。**`agent/main.py` 和 `worker/main.py` 只调 `store.initialize() → db.migrate()`；`verify()` 和 `PRAGMA integrity_check` 只有 `check.sh` 和测试在用。因此"schema 不匹配就 fail-fast"当时只覆盖"未知的更高 version"和"checksum 被改写"两种情况，一个表结构已漂移但 `schema_migrations` 行完好的库会被静默接受。

4. **checkpoint upgrader 整条链在生产不可达。**`worker/main.py` 构造 `RunCoordinator` 时从不传 `release_compatibility` → `coordinator.py` 落到空 registry → `get(key)` 恒为 `None` → 必然走 `INCOMPATIBLE_RELEASE`。全仓库只有 `test_release_checkpoint_upgrade.py` 注册过 upgrader。它属于背景说明明确排除的"滚动发布期间的 schema 兼容"，却占着约 670 行代码。

另有一处质量问题：Runtime 与 ARAG 各写了一套 SQL 语句切分器，前者用 `sqlite3.complete_statement`（正确），后者用字符串匹配 `CREATE TRIGGER`/`END;`（脆弱）。

---

## 二、要求与决策

### 2.1 用户明确给出的决策

| 议题 | 决定 |
|---|---|
| checkpoint upgrader | **删掉**，保持代码简洁 |
| schema 版本号 | **直接复用 `SCHEMA_VERSION`**，不再另立整数版本计数器 |
| 历史数据 | 不 care，`local_storage/` 已清空，不考虑任何兼容 |
| 本次执行方式 | 不跑门禁、不跑 pytest、不写新单元测试、不执行 `scripts/check.sh`；由用户自行验证 |

### 2.2 实施中锁定的两个设计决定

**ARAG 的 `SCHEMA_VERSION` 各服务自持。**`arag/` 对 `agent/` 零依赖（无任何 `from agent` import），不能为了共用一个常量破坏服务边界。Runtime 复用 `agent/runtime/domain/models.py` 的 `SCHEMA_VERSION`（`adapters/ → domain/` 是六边形允许方向）；ARAG 在 `arag/persistence/repository.py` 自持 `SCHEMA_VERSION = "1"`。共同点是**都用字符串版本、都不再有整数 migration 计数器**。

**不做 live schema 漂移检测。**`schema_checksum` 只覆盖 `schema.sql` 文件字节，能检出"当前代码的 schema ≠ 建库时的 schema"，检不出"有人手工 `ALTER` 活库"。再记一份 `sqlite_master` 摘要属于收益不明确的新增复杂度（项目已彻底没有 ALTER 路径），该限制显式写进 ADR-0003 而非隐藏。

### 2.3 零改动的契约资产

- `docs/reliability/schemas/` 六份冻结 v1 JSON Schema **未改一字**（`canonical-event-v1.schema.json` 里只有 `INCOMPATIBLE_RELEASE` 终态枚举，该终态保留）。
- `RuntimeEnvelope` / `CanonicalEvent` / `WorkingState` / `CheckpointRecord` 字段未改。
- REL/FI 编号未新增，`check.sh` 的 `REL-001..030`、`FI-01..12` 门禁未触发。

---

## 三、实施方案

分四步，每步独立可验证：

1. **删除 checkpoint upgrader** —— 与 schema 无关，最干净的一刀，先做。
2. **Runtime 单 schema** —— 含新建共享 helper。
3. **ARAG 单 schema** —— 复用同一个 helper。
4. **门禁脚本、现有测试适配、文档同步**。

目标形态：每个数据库一份 `schema.sql`，启动时要么在空库上一次性建全表，要么校验 identity 通过，否则 fail-fast 让使用者自己删库；`INCOMPATIBLE_RELEASE` 保留为无条件 fail-closed 终态。

---

## 四、实际改动

### 4.1 Step 1：删除 checkpoint upgrader

**整文件删除**

| 文件 | 行数 |
|---|---|
| `agent/runtime/ports/release_compatibility.py` | 50 |
| `agent/runtime/application/release_compatibility.py` | 32 |
| `tests/reliability/test_release_checkpoint_upgrade.py` | 357 |

**局部删除**

- `agent/runtime/adapters/sqlite/store.py`：`publish_checkpoint_upgrade` 整个方法（164 行）。它只调用 `_assert_fencing` / `_require_run_row` / `conflict` / `_append_in_tx` / `stable_id` / `canonical_json`，这些都被别处复用，删除后无孤儿。
- `agent/runtime/ports/store.py`：端口声明（8 行）。
- `agent/runtime/application/coordinator.py`：`ReleaseCompatibilityRegistry` 与三个 `CheckpointUpgrade*` import、import 列表里的 `SCHEMA_VERSION`（`RuntimeFault` 仍在用，保留）、`release_compatibility` 构造参数与赋值、分支 4 的 upgrader 半边。

**coordinator 分支 4 重写。**从约 80 行降到 16 行，并且判定不再需要 checkpoint，因此**提到 `latest_checkpoint()` 之前**——不匹配时连查 checkpoint 都省掉：

```python
adapter = self.registry.get(run.envelope.engine)
if adapter.release_fingerprint != run.envelope.release_fingerprint:
    terminal = await self.store.finalize_failure(..., terminal_status=RunStatus.INCOMPATIBLE_RELEASE, ...)
    return terminal.status
checkpoint = await self.store.latest_checkpoint(run.envelope.run_id)
```

**两条被强化的不变量**

- `UPDATE runs SET release_fingerprint=...` 全仓库只存在于被删方法内，因此 **`runs.release_fingerprint` 自 admission 起彻底不可变**，与 `engine` 同级冻结。
- release 不一致的结论从"有 upgrader 则升级，否则 incompatible"收窄为**无条件 `INCOMPATIBLE_RELEASE`**。

### 4.2 Step 2：Runtime 单 schema

**新增 `common/sqlite_schema.py`（141 行）**

对外三个符号：`SchemaIdentityError`、`schema_checksum(schema_sql)`、`ensure_current_schema(conn, *, schema_sql, schema_version, db_path, label)`。

核心逻辑在单个 `BEGIN IMMEDIATE` 内：

```
n = SELECT count(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'
n == 0              → 逐条执行 schema_sql + INSERT schema_meta(1, version, checksum, now)
有表但无 schema_meta  → SchemaIdentityError（陌生库，不猜不补）
有 schema_meta       → version/checksum 任一不符 → SchemaIdentityError
```

三个关键点：

1. **并发 bootstrap 竞态结构性消失。**旧实现要在拿到写锁后二次检查版本，因为 API 与 Worker 会同时 bootstrap 空库。把"判空 + 建表 + 写 meta"放进同一个 `BEGIN IMMEDIATE` 后，第二个进程阻塞在写锁上、醒来直接走校验分支。SQLite 支持事务性 DDL，这是原子的。
2. **事务显式管理**，用 `conn.execute("BEGIN IMMEDIATE")` + `commit/rollback`，不依赖连接的 `isolation_level`——Runtime 用 `isolation_level=None`，ARAG 用默认 `""`，helper 必须兼容两者。
3. **SQL 切分统一用 `sqlite3.complete_statement`**（`sqlite3_complete()` 是 trigger-aware 的，知道 `CREATE TRIGGER ... BEGIN ... END;` 内部分号不结束语句）。ARAG 那个脆弱的字符串匹配实现被丢弃。

fail-fast 消息含库路径、期望/实际 version+checksum，以及可直接复制的删除命令（WAL 会产生 `-wal`/`-shm`，一并列出）：

```
runtime database checksum mismatch: /abs/path/runtime.db
  expected: schema_version=1 checksum=b5e5caa0...
  found:    schema_version=1 checksum=tampered
This project does not migrate databases. Delete it and restart:
  rm -f /abs/path/runtime.db /abs/path/runtime.db-wal /abs/path/runtime.db-shm
```

**新增 `agent/runtime/adapters/sqlite/schema.sql`（265 行）**

= `001_runtime.sql` 全文 + 折叠 002/003 两列 + `schema_meta` 表。两个折叠列的 `DEFAULT` **去掉**了，只保留 `NOT NULL`——它们本来就是 `ALTER TABLE ADD COLUMN NOT NULL` 的产物，留着只会把"漏写列"从报错变成静默取默认值：

- `runs.trace_id TEXT NOT NULL`
- `tool_executions.supports_reconcile INTEGER NOT NULL CHECK (supports_reconcile IN (0,1))`

**改写 `database.py`（114 行改动）**

删除 `migrate` / `_migration_rows` / `_verify_rows` / `_split_sql` / `verify`（约 97 行），`_migrations_dir` → `_schema_path`，新增 `ensure_schema()`：开连接 → 调 `ensure_current_schema` → 捕获 `SchemaIdentityError` 并转成本模块的 `SchemaCompatibilityError`。`connect()` / `read()` / `transaction()` 原样保留，包括那段 WAL bootstrap 重试（它解决的是另一个问题）。

`PRAGMA integrity_check` **不进启动路径**（有成本），只留在 `check.sh`。

`SqliteRuntimeStore.initialize()` 改调 `ensure_schema()`，方法名不变 ⇒ `agent/main.py`、`worker/main.py` 及十几个构造 `RuntimeDatabase` 的测试文件**全部无需改动**。

**删除** `agent/runtime/adapters/sqlite/migrations/` 整个目录（259 行）。

### 4.3 Step 3：ARAG 单 schema

- **新增** `arag/persistence/schema.sql`（87 行）= 原 `MIGRATIONS[0].sql` 全文 + `schema_meta`（此库现有表不带 `STRICT`，保持一致）。
- **删除** `arag/persistence/migrations.py`（105 行）。
- `arag/persistence/repository.py`：加 `SCHEMA_VERSION = "1"`；`initialize()` 从 41 行降到约 16 行；删除 `schema_version()`（调用方只有 `check.sh` 和一处测试断言，无生产调用方）；删除 `_split_sql_script`（20 行）。
- `arag/context.py` 的 `await repository.initialize()` 未改。

### 4.4 Step 4：门禁、测试、文档

**`scripts/check.sh`**

- 删除 `from arag.persistence.migrations import LATEST_SCHEMA_VERSION`。
- `migrate(); verify(); verify()` → `ensure_schema()` 调两次（第二次走校验分支）+ 显式 `PRAGMA integrity_check`（原先这个检查藏在被删的 `verify()` 里）。
- 删除 `schema_version() != LATEST_SCHEMA_VERSION` 断言；ARAG 侧原有的 `integrity_check` 保留。
- 末段旧协议扫描的 `patterns` 未动。

**现有测试（只做 API 适配，未新增覆盖面）**

| 文件 | 处理 |
|---|---|
| `test_release_checkpoint_upgrade.py` | 整文件删除 |
| `test_runtime_migrations.py` → `test_runtime_schema.py` | 保留并适配并发 bootstrap 用例；删除"v1 库升级到 002/003"用例（它验证的正是要移除的兼容层）；checksum 用例改为篡改 `schema_meta`；补一个"有表但无 `schema_meta`"用例，因为这是新分支 |
| `test_rag_persistence.py` | 删 `schema_version() == 1` 断言；`test_migration_checksum_rewrite_fails_fast` → `test_schema_identity_rewrite_fails_fast` |
| `test_release_fingerprint_coverage.py` | 字面路径 `.../migrations/001_runtime.sql` → `.../schema.sql` |

REL-028 在 `implementation-coverage.md` 原钉 7 个节点，其中 4 个来自被删文件；剩余 3 个真实存在，`check.sh` 的"每行至少一个真实 pytest 节点"继续满足。

**文档同步（12 份）**

`ADR-0003 §3`（重写为单一 schema + identity 校验 + 显式声明不做漂移检测）、`ADR-0005 §2/§3/§4/§5/Consequences`（删除 upgrader 协议整节，resume matrix 收窄为两行）、`ADR-0007 §1`（去掉对 `003_run_trace_id.sql` 的引用）、`docs/reliability/README.md`、`state-machines.md`、`failure-matrix.md`、`reliability-test-catalog.md`、`implementation-coverage.md`、`README.md`、`RUNBOOK.md`、`AGENTS.md`、`CLAUDE.md`。

---

## 五、验证情况

### 5.1 已完成的自检（非门禁）

- `py_compile` 覆盖 `agent arag common skillcenter a2a_service tests` 全部 `.py`，通过。
- `agent.main`、`agent.runtime.worker.main`、`arag.context` 均可正常 import。
- 手工 smoke（临时目录）：
  - Runtime 建库得到 16 张表 + `schema_meta` 单行 + `integrity_check ok`；两个折叠列均为 `NOT NULL` 且无 default。
  - API/Worker 并发 bootstrap 同一空库，`schema_meta` 恰好 1 行。
  - 三条 fail-fast 分支（checksum 被改写 / version 不符 / 有表但无 `schema_meta`）均抛错并打印 `rm -f` 指令。
  - ARAG 建库含 `chunks_no_update` trigger，`integrity_check ok`，篡改 checksum 后 fail-fast。
- 残留扫描：`schema_migrations` / `publish_checkpoint_upgrade` / `release_compatibility` / `LATEST_SCHEMA_VERSION` / `001_runtime` / `.migrate()` / `.verify()` 在 `agent/ arag/ common/ tests/ scripts/ eval/ web/` 下**零命中**。

### 5.2 待用户执行的验证

```bash
bash scripts/check.sh          # 全量门禁
bash scripts/run_all.sh        # 空 local_storage 冷启动五进程全链路
```

手工验证 fail-fast 不会静默迁移（改一个字节后启动 Worker，应报错退出并打印 `rm -f` 指令）：

```bash
.venv/bin/python -c "import sqlite3;c=sqlite3.connect('local_storage/runtime/runtime.db');c.execute(\"UPDATE schema_meta SET schema_checksum='tampered'\");c.commit()"
.venv/bin/python -m agent.runtime.worker.main
```

---

## 六、遗留事项

1. **`sxw_aicoding/` 下的阅读笔记有失效引用**（本次未改，属自有笔记而非代码资产，且不在 `check.sh` 扫描根内）：
   - `代码阅读指南/Conversation-Run-Activity层次模型详解.md`、`代码阅读指南/Runtime核心数据模型详解.md` 仍指向 `migrations/001_runtime.sql`，应改为 `schema.sql` 并重新核对行号。
   - `代码阅读指南/CreateRun全链路代码阅读指南.md`、`代码阅读指南/全链路整理/Query到Answer全链路代码阅读指南.md`、`代码评价/对于_execute_claim代码评价.md` 仍描述 checkpoint upgrader 流程。
2. **schema 漂移检测**是有意不做的（见 2.2），已写入 ADR-0003。若将来真的需要，应作为独立改动并给出明确收益论证。
3. 本次按用户指示未新增测试规模；`test_runtime_schema.py` 里的"有表但无 `schema_meta`"用例是为新分支补的最小覆盖，如不需要可直接删除。
