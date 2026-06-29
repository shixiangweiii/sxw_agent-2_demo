# 07 · SKILL：claude-skill 沙箱执行（精简复刻）  ✅ 已完成

## Context

复刻 agent-2 的 **claude-skill** 能力：把一个技能包（SKILL.md）作为**子代理在沙箱中执行**，
沙箱按 **provider 抽象**（本地可跑 / AgentBay 云桩）。这是 agent runtime「代码执行 / 沙箱技能」的核心形态。

### 真实源码映射
| demo | 真实 |
|---|---|
| `agent/claude_skill/sandbox/base.py` | `app/core/claude_skill/sandbox/base_sandbox.py`（`BaseSandbox`/`EnumSandboxProvider`/`EnumSandboxScene` + file/shell/code 服务）|
| `sandbox/local_sandbox.py` / `agentbay_sandbox.py` / `factory.py` | `sandbox/agentbay/*` + provider 抽象 |
| `toolset.py` | `toolset/skill_remote_sandbox_toolset.py` |
| `skill_runner.py` | `skill_runner.py` / `skill_agent_builder.py` |
| `claude_skill_tool.py` | `single_loop/claude_skill_tool.py`（两契约）|

## 设计

```
主代理 ──调用──▶ ClaudeSkillTool(claude_skill_<id>)
                    │ build_sandbox(provider)  [LOCAL 可跑 / AGENT_BAY 桩]
                    ▼
              run_skill：LlmAgent(SKILL.md 指令 + 沙箱工具集) 经 Runner 跑
                    │  沙箱工具：read_file/write_file/list_files/run_shell/run_python（子进程）
                    │  ── 两契约 ──
                    ├─ UI：子代理事件 → ui_event_queue → skill_event（实时流出，复用既有 merge）
                    └─ LLM：最终文本 → {output} 回父 LLM
```

- **沙箱 provider 抽象**：`BaseSandbox` + `EnumSandboxProvider(LOCAL, AGENT_BAY)` + `EnumSandboxScene(CODE/FILE/SHELL)`；`FileService`/`ShellService`/`CodeService` 三接口。
- **LocalSandbox**（可跑）：`tempfile` 工作目录；file 读写限制在工作目录内；shell/python 走 `asyncio` 子进程 + 超时。
- **AgentbaySandbox**（桩）：`try_create`/服务方法 raise `SandboxUnavailableError`——演示 provider 抽象与可切换（生产换 wuying-agentbay-sdk）。
- **零改引擎**：claude-skill 事件推既有 `ui_event_queue`，引擎 `merge_runner_events` 已并发 drain。
- 内置技能：`skills_data/data_analysis/SKILL.md`（沙箱跑 numpy/pandas 做统计）。

## 实施记录（E2E 通过）
- `py_compile` 全绿；LocalSandbox 直跑 `run_python` 算 `[12,7,9,20]` 均值 12.0 方差 24.5（真实 numpy）；AgentBay 桩抛 `SandboxUnavailableError`。
- E2E：`ENGINE=agent_loop`「用数据分析技能算 12,7,9,20 的均值方差」→ `claude_skill_data_analysis` → 沙箱子代理 `run_python` → `skill_event`(子 tool_call/result/text 流) → 父最终答 均值12.0/总体方差24.5/样本方差32.67。
- 文件：`agent/claude_skill/{__init__, sandbox/{base,local_sandbox,agentbay_sandbox,factory}, toolset, catalog, skill_runner, claude_skill_tool, skills_data/data_analysis/SKILL.md}`；接线 `config.py`(SANDBOX_PROVIDER) + `context.attach_claude_skill_tools` + `main`。

## 刻意裁剪
oss/computer/browser/artifact/context 服务、network_control、auth、agui、kinto runner、resume；真实 AgentBay SDK（桩代替）。LocalSandbox 仅演示用、**非生产隔离**（仅工作目录限制 + 超时）。
