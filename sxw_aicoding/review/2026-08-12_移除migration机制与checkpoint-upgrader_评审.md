# 代码评审：移除 schema migration 机制与 checkpoint upgrader

- 评审日期：2026-08-12
- 评审对象：当前工作树未提交改动（28 个跟踪文件 `+111/-1384`，4 个未跟踪新文件）
- 对照 changelog：`sxw_aicoding/changelog/2026-08-12_移除migration机制与checkpoint-upgrader.md`
- 对照约束：`sxw_aicoding/项目背景说明.txt`、`AGENTS.md`、`CLAUDE.md`、`docs/reliability/`
- 本次评审只读，未修改任何文件

---

## 〇、结论

**方向正确，实现基本干净，可以接受。** 改动准确命中背景说明第 7–9 行的硬约束，删掉的三套机制（Runtime 增量 migration、ARAG 增量 migration、checkpoint 跨 release upgrader）确实全部属于"旧库兼容 / 滚动发布期 schema 兼容"，都在项目范围之外；净减 ~830 行且没有留下孤儿代码。

changelog 里最关键的两条技术判断经复核**属实**：

1. 原 `verify()` 第一句就是 `await self.migrate()`，对落后库确实是"先改写再宣布验证通过"，与"程序不得静默迁移旧数据库"直接冲突。现在这条路径已彻底消失。
2. checkpoint upgrader 整条链在生产不可达（`worker/main.py` 从不传 `release_compatibility`），~670 行只被一个测试文件驱动。删除是对的。

需要处理的问题集中在**失败路径的错误语义和运维文档**，不在核心事务逻辑上。核心事务逻辑我实测通过。

---

## 一、已实测验证通过的部分

以下不是读代码得出的推断，是本次评审实际跑出来的结果。

| 验证项 | 方法 | 结果 |
|---|---|---|
| 全量门禁 | `bash scripts/check.sh` | **PASS** |
| 可靠性测试 | `pytest tests/reliability -q` | **241 passed** |
| Runtime schema 折叠是否忠实 | `diff` 旧 `001_runtime.sql` 与新 `schema.sql` | 仅差 `schema_meta` + 折叠的两列，无其他漂移 |
| ARAG schema 折叠是否忠实 | 对比 `MIGRATIONS[0].sql` | 全文一致 + `schema_meta`（无 `STRICT`，与该库其余表一致） |
| 折叠列去掉 `DEFAULT` 是否安全 | 查 `store.py` 的 `INSERT INTO runs` / `INSERT INTO tool_executions` | 两列均显式赋值，去掉 `DEFAULT` 后"漏写列"会直接报错而不是静默取默认值——这个决定是对的 |
| 残留引用 | 在 `agent/ arag/ common/ tests/ scripts/ eval/ web/ docs/` + 六份根文档搜 `schema_migrations` / `LATEST_SCHEMA_VERSION` / `publish_checkpoint_upgrade` / `release_compatibility` / `001_runtime` / `.migrate()` / `.verify()` / `schema_version()` / `CheckpointUpgrade` | **零命中** |
| 并发 bootstrap（changelog 的核心论点） | **6 个真实 OS 进程**同时对同一个空库调 `ensure_schema()` | 全部成功；`schema_meta` 恰好 1 行、16 张表、`integrity_check ok` |
| 陈旧 Worker 不得裁决终态 | 读 `finalize_failure` | 内部自带 `_assert_fencing`，删掉 upgrader 分支里那段显式 `STALE_FENCING_TOKEN`/`CHECKPOINT_REVISION_CONFLICT` 重抛后，不变量仍由 `finalize_failure` 保证，**没有破口** |
| `runs.release_fingerprint` 是否真的不可变 | 全仓搜 `UPDATE runs SET release_fingerprint` | 零命中，admission 后确实与 `engine` 同级冻结，与新 ADR-0005 §2 一致 |
| 冻结契约 | `docs/reliability/schemas/` 六份 v1 Schema | 未改一字；`INCOMPATIBLE_RELEASE` 枚举保留；REL/FI 编号未动 |
| 漂移检测的真实边界 | 在已建好的库上 `CREATE TABLE injected` + `ALTER TABLE runs ADD COLUMN sneaky` 后重启校验 | **通过校验（被接受）** —— 与 ADR-0003 §3 的声明完全一致，属于已显式披露的限制，不是隐藏缺陷 |

**设计上真正的收获**（值得写进面试材料）：把"判空 + 建表 + 写 meta"塞进同一个 `BEGIN IMMEDIATE`，让并发 bootstrap 的竞态**结构性消失**，而不是靠拿锁后二次检查去补。旧实现需要在拿到写锁后重查版本号，是因为 DDL 与 meta 写入不在一个事务里；新实现依赖 SQLite 的事务性 DDL，第二个进程阻塞在写锁上、醒来直接落到校验分支。这是"用更强的原语消掉一类 bug"，比"多加一次检查"高一个层次。6 进程实测印证了这一点。

---

## 二、需要修的问题

### P1-1　`_read_meta` 的 `except sqlite3.OperationalError` 过宽，可能把 I/O 错误翻译成"删库"指令

`common/sqlite_schema.py:81-90`

```python
try:
    row = await (await conn.execute(
        "SELECT schema_version,schema_checksum FROM schema_meta WHERE id=1"
    )).fetchone()
except sqlite3.OperationalError:
    # No schema_meta table at all: some other database owns this file.
    return None
```

注释假设 `OperationalError` **只可能**是"表不存在"，但 SQLite 把 `SQLITE_IOERR`（disk I/O error）、`SQLITE_BUSY`、被中断的读也映射到 `OperationalError`。一旦命中，`_read_meta` 返回 `None` → `_verify` 走 `meta is None` 分支 → 操作者拿到：

```
runtime database schema_meta is missing: /.../runtime.db
This project does not migrate databases. Delete it and restart:
  rm -f /.../runtime.db /.../runtime.db-wal /.../runtime.db-shm
```

**失败场景**：磁盘出现瞬时 I/O 错误 → API 启动报"你的库不是本项目的库" → 操作者照指令执行 `rm -f` → 一个完全健康、可能带着未终态 Run、canonical event 和 artifact link 的 `runtime.db` 被销毁。

概率低，但爆炸半径是这次改动**唯一绝对不能出现的失败模式**：整个设计的立足点就是"程序不得静默删除，由使用者显式删除"。把一条错误的 `rm` 递到使用者手里，等价于绕过了这条约束。

**修法（3 行，且比现在更精确）**：`_user_table_count` 已经在查 `sqlite_master`，把它改成查表名集合，直接判断 `"schema_meta" in names`，然后彻底删掉这个 `except`。这样"表不存在"由数据判定，任何真实 `OperationalError` 原样上抛——启动失败时操作者看到的是 `disk I/O error`，而不是一条删库建议。

### P1-2　报错建议 `rm -f`，与 `RUNBOOK.md` §6 明确要求的 `mv` 备份相矛盾

`common/sqlite_schema.py:119` 生成 `rm -f <db> <db>-wal <db>-shm`；而 `RUNBOOK.md:180-186` 写的是：

> 需要干净初始化时，先停全部进程，再把整个运行目录移走留作本机备份：
> `[ ! -e local_storage ] || mv local_storage "local_storage.pre-r4.$(date +%Y%m%d-%H%M%S)"`

同一个动作，运维手册要求**非破坏性 `mv` 留证据**，代码却主动教人 `rm`。两者都能解决 schema 不匹配，但 `mv` 严格占优：代价相同，却保住了事后复盘的现场。schema 不匹配往往正是"刚改完 schema 忘了这库里还有东西"的时刻，恰恰最需要保留现场。

`rm -f` 那份把 `-wal`/`-shm` 一起列出来是对的（RUNBOOK 也警告过不要只删主文件），问题只在动词。

**修法**：把建议改成 `mv` 到带时间戳的备份名，或直接指向 `RUNBOOK.md` §6。二选一即可，不要两套语义并存。

### P1-3　`RUNBOOK.md` 未同步本次改动最面向运维的那个行为

本次改动给 API/Worker 增加了一个**全新的启动期硬失败**：schema identity 不符时进程直接退出。这是操作者最可能撞上的新现象。

已同步：`failure-matrix.md`（新增 "DB schema identity 不符" 行）、`README.md`、`AGENTS.md`、`CLAUDE.md`、四份 ADR。
**未同步**：`RUNBOOK.md` §13「故障与恢复操作」和 §14「常见排障」两张表都没有对应行。§6 有正确的重置流程，但没有任何一处把"进程启动即退出 + schema identity 报错"这个**症状**连到 §6 这个**处置**上。

`AGENTS.md` / `CLAUDE.md` 的改动规则都写了"改能力边界要同步 RUNBOOK"，而 `check.sh` 抓不到这类遗漏——只能靠人。

**修法**：§14 加一行即可，例如
`| 进程启动即退出并报 schema identity | 本地库不是当前 schema；按 §6 备份后重建，不要手工改表 |`

---

## 三、建议修的问题（成本极低）

### P2-1　`_split_sql` 用 `SchemaIdentityError` 报告"仓库自带资产坏了"

`common/sqlite_schema.py:140` 在 `schema.sql` 结尾是残缺语句时抛 `SchemaIdentityError`。两个调用方都会把它翻译成**磁盘上数据库的问题**：`RuntimeDatabase.ensure_schema` → `SchemaCompatibilityError`；`RagRepository.initialize` → `RagSchemaError`，而后者的 docstring 写的是 *"The on-disk schema is newer than, or differs from, this release."*

于是"我们自己的 `schema.sql` 写错了"会被报成"你的数据库不对"。用 `ValueError` 之类的独立类型即可。

顺带：`_split_sql` 是纯字符串处理，可以提到 `BEGIN IMMEDIATE` **之前**——资产坏了就不必先去抢写锁。

### P2-2　`RagSchemaError` docstring 已失效

`arag/persistence/repository.py:41`："The on-disk schema is newer than, or differs from, this release."

`newer than` 在新模型里不再有意义：已经没有版本序，只有相等/不等。一行的事。

### P2-3　"`schema_meta` 行被清空"与"陌生数据库"给出同一条报错

实测：把 `schema_meta` 的行 `DELETE` 掉，报错文案与"库里只有一张无关表"完全相同，都是 `schema_meta is missing`。

这两种情况的诊断方向不同——前者是自己的库被截断/误操作，后者多半是**路径配错指到了别的库**（这个更常见，也更不该删）。多一个分支就能区分，值得加。

### P2-4　`REL-028` 覆盖行丢了正向路径节点

原行钉 7 个节点，其中 4 个在被删文件里，包括 `test_rel_28_exact_release_resumes_without_running_upgrader`——REL-028 描述的第一句"release/schema 完全匹配才可恢复"。现在剩下的 3 个节点分别是"不匹配收口"、"三个 active pointer 原子发布"、"fingerprint 源覆盖"，**没有一个断言"匹配时能从 committed checkpoint 恢复"**。

行为本身仍有覆盖（如 `test_native_kernel_recovery.py::test_native_kernel_recovers_durable_tool_boundary_without_duplicate_effect`、`test_runtime_core.py::test_rel_13_expired_lease_recovered_only_once`），只是覆盖矩阵不再指向它。`check.sh` 只校验"每行至少一个真实节点"，所以这个缺口是静默的。把上述任一现有节点补进 REL-028 行即可，零成本。

### P2-5　三个新文件仍未 `git add`

`common/sqlite_schema.py`、`agent/runtime/adapters/sqlite/schema.sql`、`arag/persistence/schema.sql` 目前是 untracked。`.gitignore` 没有排除它们，纯粹是还没暂存——但在提交前，这份改动对任何其他人都是跑不起来的。

---

## 四、设计取舍：认为合理，但建议显式写下来

这些不是缺陷，是需要被记住的性质。

**1. checksum 覆盖文件字节，且 `schema.sql` 同时在 release fingerprint 里。**
`_RUNTIME_SOURCES = ("agent/runtime",)` 覆盖 `.sql`（`test_release_fingerprint_coverage.py:80` 已正确改为断言 `schema.sql`）。因此**只改 `schema.sql` 的一行注释**会同时触发两件事：所有本地库 identity 失配需删库重建；release fingerprint 变化，所有未终态 Run 收口 `INCOMPATIBLE_RELEASE`。

两者都是 fail-closed，方向正确，也不是本次引入的（旧 `001_runtime.sql` 同样在 `agent/runtime` 下）。但删掉 upgrader 之后**再无任何逃生口**，这个联动的后果变重了。ADR-0005 已经给出唯一正确的操作建议（"在没有未终态 Run 时再升级代码"），建议在 ADR-0003 §3 补一句点明这个双重效应，别让后来者踩到才发现。

**2. `schema_meta.schema_version` 复用了冻结契约的 `SCHEMA_VERSION`。**
用户明确拍板复用，没问题。只需认识到：checksum 已经精确锁死 DDL，version 字段不再携带独立信号——改 DB schema 不会让它变，改契约版本反而会作废所有库。它现在唯一的实际作用是让报错更好读。这是可接受的，但不要误以为它在做版本兼容判定。

**3. `common/sqlite_schema.py` 不在 release fingerprint 内。**
改动前所有 schema 逻辑都在 `agent/runtime/` 下（被 fingerprint 覆盖），现在一部分挪到了 `common/`，而 `_INTEGRATION_SOURCES` 只列了 `common/skill_contract.py`。**没有语义损失**——该模块只做建库/校验，不参与 checkpoint 解释、Tool 身份或 committed event，真正的 DDL（`schema.sql`）仍被覆盖。记录在此仅为完整性。

**4. 校验分支也要拿写锁。**
`ensure_current_schema` 在判空之前就 `BEGIN IMMEDIATE`，所以即使是"只读校验"的启动也会争一次写锁。极端情况下，Worker 正在持续写时重启 API，可能在 `busy_timeout`（默认 5000ms）后以 `sqlite3.OperationalError: database is locked` 启动失败——注意这条**不会**被包装成 schema 错误，操作者看到的是正确的"库被锁"，这点是对的。

改成"先只读判空、为空再拿写锁"会把被结构性消掉的二次检查请回来。用一次可忽略的启动争锁换掉一整类竞态 bug，这笔账划算，维持现状。项目本身还有"SQLite 写事务必须短"的硬不变量兜底。

**5. 服务边界处理正确。**
ARAG 自持 `SCHEMA_VERSION = "1"`、不 import `agent`，Runtime 从 `domain/models.py` 取（`adapters/ → domain/` 是六边形允许方向），二者共用 `common/`。既没有为了省一个常量捅穿服务边界，也没有把同一份逻辑抄两遍。继承层级仍是 `Base -> Sub` 两层以内。符合 `AGENTS.md`。

---

## 五、流程问题

**`sxw_aicoding/项目背景说明.txt` 被改了，但 changelog 没提。**

删掉的是那条过渡指令（"现有 migration 机制属于待移除的历史实现……后续通过独立、可验证的重构，将其替换为单一的当前 schema 初始化和版本/checksum 校验"）。改动本身是对的——过渡已完成，留着会误导。

但 changelog §4.4 的"文档同步（12 份）"清单里没有它。**这是用户自己的需求源文件**，是判定其他一切改动是否合规的基准，改它必须显式声明，不能混在文档同步里悄悄带过。以后动这个文件应该单独列出来。

---

## 六、修复优先级

| 优先级 | 项 | 位置 | 成本 |
|---|---|---|---|
| P1 | 收窄 `_read_meta` 的异常捕获，改为查 `sqlite_master` 表名 | `common/sqlite_schema.py:81-90` | ~3 行 |
| P1 | 报错建议改 `mv` 备份或指向 RUNBOOK §6 | `common/sqlite_schema.py:119` | 1 行 |
| P1 | RUNBOOK §14 补启动期 schema fail-fast 排障行 | `RUNBOOK.md` | 1 行 |
| P2 | `_split_sql` 换独立异常类型，并提到事务外 | `common/sqlite_schema.py:123-141` | 数行 |
| P2 | `RagSchemaError` docstring 去掉 `newer than` | `arag/persistence/repository.py:41` | 1 行 |
| P2 | 区分"`schema_meta` 无行"与"陌生库" | `common/sqlite_schema.py:93-120` | 1 个分支 |
| P2 | REL-028 补回一个正向恢复节点 | `docs/reliability/implementation-coverage.md:46` | 1 处 |
| P2 | `git add` 三个新文件 | — | — |
| P3 | ADR-0003 §3 点明 checksum 与 release fingerprint 的双重效应 | `docs/reliability/adr/0003-sqlite-authority.md` | 1 句 |

P1 三项都是失败路径与运维文档，改完这次改动可以认为收口。核心事务逻辑不需要动。

---

## 七、遗留事项复核

changelog §六列出的三条遗留事项，复核后确认：

1. `sxw_aicoding/代码阅读指南/`、`代码评价/` 下的失效引用**确实存在且未修**。不在 `check.sh` 扫描根内，不影响门禁，属自有笔记债。建议归入独立小任务，不要塞进本次。
2. 不做 live schema 漂移检测——已实测确认现状如此，且已写入 ADR-0003 §3，**属于正确披露而非隐藏**。
3. `test_runtime_schema.py` 的"有表但无 `schema_meta`"用例是新分支的最小覆盖，**建议保留**：这条路径正好是 P1-1 和 P2-3 涉及的分支，删掉会让上述修复失去回归保护。
