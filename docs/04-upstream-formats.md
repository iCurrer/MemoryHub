# 上游记忆来源基线（实测 + 官方文档）

> 建立日期：2026-11-24 ｜ 维护：当次改动人 ｜ 用途：**P2-03 / P2-04 / P2-05 的唯一格式依据**

本文件回答一个问题：**Codex、Claude Code、Cursor、WorkBuddy、Trae 这些工具，把"记忆"和"指令"存在哪、存成什么格式。**

它不是策划书的一部分。策划书 `docs/reference/plan-v0.1.md` §4.2 对适配边界的描述是**立项时的猜测**，本文是核实结果，冲突时以本文为准。

## 0. 置信度定义

| 标记 | 含义 |
|---|---|
| **实盘** | 在本机文件系统上实际看到文件、读到结构、统计过行数/键名 |
| **官方** | 厂商官方文档明确写明 |
| **镜像** | 官方页抓取失败，使用第三方镜像的官方文档副本（内容一致但可能滞后一个版本） |
| **推测** | 没有证据，仅为假设。**不得据此写代码** |

## 1. 结论摘要

| 工具 | 指令层 | 记忆层 | 格式 | 置信度 |
|---|---|---|---|---|
| Codex | `$CODEX_HOME/AGENTS.md`（默认 `~/.codex`）+ 项目目录链 | `~/.codex/memories_1.sqlite`、`~/.codex/sessions/**/rollout-*.jsonl.zst` | Markdown / **SQLite** / JSONL+zstd | 实盘 + 镜像 |
| Claude Code | `~/.claude/CLAUDE.md`、`./CLAUDE.md`、`./CLAUDE.local.md`、`.claude/rules/*.md` | `~/.claude/projects/<project>/memory/`（`MEMORY.md` + 主题文件） | Markdown（记忆带 YAML frontmatter） | 官方 |
| Cursor | `.cursor/rules/*.mdc`（**只认 `.mdc`**）、用户级 / 团队级规则、`AGENTS.md` | 无独立记忆层 | Markdown + frontmatter | 官方 |
| WorkBuddy | 未见（未验证） | `~/.workbuddy/memory/<uuid>_memory.md`（+ `.bak`） | Markdown，**无 frontmatter** | 实盘 |
| Trae CN | `~/.trae-cn/skills/`（未验证是否算指令层） | `~/.trae-cn/memory/` 三层 | Markdown + **JSONL** | 实盘 |

**一句话**：`AGENTS.md` 是**指令**，不是记忆。把两者混为一谈，是本项目最容易犯的第二个设计错误（第一个是回写分发，见 ADR-002）。

---

## 2. Codex

### 2.1 指令层是一条带覆盖语义的链（镜像）

来源：[官方页的镜像副本](https://github.com/mehmetbaykar/codex-docs-skill/blob/main/skills/codex-docs/references/agent-configuration__agents-md.md)（`learn.chatgpt.com` 直接抓取失败）。

发现顺序（每次启动重建一次，无缓存）：

1. **全局**：`$CODEX_HOME`（默认 `~/.codex`，可用 `CODEX_HOME` 改）下，先 `AGENTS.override.md`，没有才 `AGENTS.md`。**只取第一个非空文件。**
2. **项目**：从项目根（通常是 git root）**向下走到 cwd**，每层依次找 `AGENTS.override.md` → `AGENTS.md` → `project_doc_fallback_filenames` 里的名字。**每层最多取一个文件。**
3. **合并**：从根往下用空行拼接。**越靠近 cwd 的越靠后 → 覆盖前面的**。
4. **截断**：累计超过 `project_doc_max_bytes`（默认 **32 KiB**）就停止追加。

相关配置键（`~/.codex/config.toml`）：`project_doc_fallback_filenames`（列表）、`project_doc_max_bytes`（整数）。

> 对适配器的含义：**只存 `source_path` 不够**，必须同时记录它在链中的位置与层级，否则"谁覆盖谁"这个信息会丢。

### 2.2 记忆层不是 Markdown（实盘）

本机 `~/.codex/memories_1.sqlite`（45 KB）实际表结构：

```
_sqlx_migrations(version, description, installed_on, success, checksum, execution_time)
stage1_outputs(thread_id, source_updated_at, raw_memory, rollout_summary, rollout_slug,
               generated_at, usage_count, last_usage,
               selected_for_phase2, selected_for_phase2_source_updated_at)
jobs(kind, job_key, status, worker_id, ownership_token, started_at, finished_at,
     lease_until, retry_at, retry_remaining, last_error, input_watermark, last_success_watermark)
consolidation_progress(singleton, max_thread_count)
```

观察：

- 记忆是**从会话线程蒸馏**出来的（`thread_id` → `raw_memory` / `rollout_summary`），带 `usage_count` / `last_usage`（被召回次数与时间）—— MemoryHub 当前**没有**这两个字段。
- 有 `jobs` 表 + `lease_until` / `retry_remaining`：上游自己就有"任务队列 + 租约 + 重试"，说明"事件 → 任务"这层在别的工具里也是必需结构。
- 本机该表 **0 行**（尚未积累），结构可信但样本为零。

会话原始记录：`~/.codex/sessions/YYYY/MM/DD/rollout-<ISO时间>-<uuid>.jsonl.zst`（zstd 压缩的 JSONL，本机 9 个文件）。

### 2.3 本机实际状态

- `~/.codex/config.toml` 存在，**没有** `project_doc_fallback_filenames` / `project_doc_max_bytes` → 全部走默认。
- **`~/.codex/AGENTS.md` 不存在** → 这台机器上 Codex 的全局指令层是空的。所以"Codex 适配器 = 读 AGENTS.md"只在**项目级**成立。

---

## 3. Claude Code（官方）

来源：<https://code.claude.com/docs/en/memory>

### 3.1 指令层，按加载顺序（范围从宽到窄）

| 范围 | 位置 |
|---|---|
| 组织策略 | macOS `/Library/Application Support/ClaudeCode/CLAUDE.md`；Linux `/etc/claude-code/CLAUDE.md`；**Windows `C:\Program Files\ClaudeCode\CLAUDE.md`** |
| 用户 | `~/.claude/CLAUDE.md` |
| 项目 | `./CLAUDE.md` 或 `./.claude/CLAUDE.md` |
| 本地 | `./CLAUDE.local.md`（建议 gitignore） |
| 规则 | `.claude/rules/*.md`（可带 `paths:` frontmatter 做路径限定）；用户级 `~/.claude/rules/` |

- 层级：从 cwd **向上**逐级加载并拼接；同目录内 `CLAUDE.local.md` 排在 `CLAUDE.md` **之后**。
- 子目录里的 `CLAUDE.md` **不在启动时加载**，等 Claude 读写该目录内文件时才加载。
- `@path` 导入最多 4 跳；跨工作目录的外部导入会弹一次批准。
- 单文件超过 4 MiB 整个跳过。

### 3.2 AGENTS.md 的默认行为（容易踩）

| `Project instructions` 取值 | 实际读什么 |
|---|---|
| `claude-md-or-agents-md`（**默认**） | 有 `CLAUDE.md` 就读 `CLAUDE.md`；**没有**才读 `AGENTS.md` |
| `claude-md-and-agents-md` | 两者都读，同目录先 `CLAUDE.md` 再 `AGENTS.md` |
| `claude-md` | 只读 `CLAUDE.md` |
| `managed-only` | 只读组织策略 + auto memory |

需要 Claude Code **≥ v2.1.277**（`v2.1.281` 之前的某些会话仍读不到）。

> 对 MemoryHub 的含义：**同一份 `AGENTS.md` 在不同工具里的"生效优先级"不同**。本仓库 `AGENTS.md` + `CLAUDE.md` 并存时，Claude 只读 `CLAUDE.md`。适配器采集时若把 `AGENTS.md` 标成"已生效指令"，在 Claude 侧是错的。

### 3.3 auto memory：字段几乎与我们同构（官方）

目录：`~/.claude/projects/<project>/memory/`，`<project>` 由 git 仓库派生（同一 repo 的 worktree 共享一个目录）；可用 `autoMemoryDirectory` 覆盖；`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 关闭。

```
~/.claude/projects/<project>/memory/
├── MEMORY.md            # 索引，一行一条，启动时只加载前 200 行 / 25KB
├── user_role.md         # 一条记忆
├── feedback_testing.md  # 一条记忆
└── ...
```

- 每个记忆文件 frontmatter 有 `type ∈ {user, feedback, project, reference}`。
- Claude Code 写入时会记录 `modified`（ISO 8601 时间戳）。
- 主题文件**不在启动时加载**，Claude 按需读。
- 机器本地，不跨设备同步；不受 `cleanupPeriodDays` 清理影响。

> 对 MemoryHub 的含义：`type` ↔ 我们的 `category`、`modified` ↔ `updated_at` 可直接映射。这是**目前最容易做对**的适配器。

---

## 4. Cursor（官方）

来源：<https://cursor.com/docs/rules>

- 项目规则：`.cursor/rules/*.mdc`，**必须是 `.mdc`**；同目录下的 `.md` 会被忽略（没有 frontmatter 就无法声明 `description` / `globs` / `alwaysApply`）。
- frontmatter 三字段决定何时加载：

| `alwaysApply` | `description` | `globs` | 行为 |
|---|---|---|---|
| `true` | — | — | 每次会话都包含 |
| `false` | — | 有 | 上下文出现匹配文件时自动附加 |
| `false` | 有 | 无 | Agent 按描述自行决定 |
| `false` | 无 | 无 | 只在 `@` 提及时包含 |

- 另有 **User Rules**（Cursor 环境级）与 **Team Rules**（面板管理），本地是否落盘未验证。
- 也支持 `AGENTS.md` 作为 `.cursor/rules` 的简单替代。

---

## 5. WorkBuddy（实盘，无官方文档）

本机样本：`~/.workbuddy/memory/<profile-uuid>_memory.md`（8.7 KB，同目录另有 `.bak`，15 KB）。

> 公开仓脱敏：真实 `<profile-uuid>` 已替换为占位符，它是本机 WorkBuddy 配置目录的稳定标识符，不应随仓库公开。

结构（8.7 KB / 33 行 / 2 个标题 / 7 条 bullet）：

```markdown
# User Memory Profile
> Last updated: 2026-11-15T07:52:40+08:00
> Version: 23

## Memory Block
- ...
```

特征：

- **无 YAML frontmatter**，元数据写在块引用里（`Last updated` + `Version`）。
- `Version` 单调递增 → 上游自己维护版本号，和 Git 的版本历史语义**重叠**。
- 一个 `<uuid>_memory.md` 疑似"一个用户/工作区一份"，文件名是 uuid 而不是可读标识。

> 未验证：`<uuid>` 代表用户还是工作区；`## Memory Block` 内部有无固定子结构。

---

## 6. Trae CN（实盘）

本机 `~/.trae-cn/memory/` 的实际分层：

```
user_profile.md                                   # 2 行，1 个标题（全局偏好）
projects/<slug>/project_memory.md                 # 3~11 个标题（项目级，如 Hard Constraints /
                                                  #   Engineering Conventions / Lessons Learned）
projects/<slug>/<YYYYMMDD>/session_memory_<id>.jsonl   # 逐会话，JSONL
projects/<slug>/<YYYYMMDD>/topics.md              # 会话摘要，首行是 [session_id: ... | topic_summary_time: ...]
```

`session_memory_*.jsonl` 的记录键（实测）：

```
intent(str)  actions(list)  outcome(str)  learned(list)
message_id(str)  message_summary_time(str)  compact_summary_meta(dict)
```

`<slug>` 是由路径派生的编码，例如 `-d-Android-LENS-Gallery--p2-dcbf569fbda07c7620a5`，与 Claude Code 的 `<project>` 目录同一思路。

> 对 MemoryHub 的含义：`intent` / `outcome` / `learned` 是**结构化记忆**，比 `AGENTS.md` 的段落更接近"用户记忆"。这类来源应当映射为 `source_kind=session_distill`，而不是 `instruction`。

---

## 7. 对 MemoryHub 设计的三个后果

### 7.1 `source_kind` 的 4 个枚举不够（待拍板，ADR-011）

现有：`instruction` / `memory` / `transcript` / `doc`。`memory` 太笼统，建议细化为：

| 建议值 | 覆盖 | 例子 |
|---|---|---|
| `instruction` | 给 AI 的规则/偏好指令 | Codex `AGENTS.md`、Claude `CLAUDE.md`、`.cursor/rules/*.mdc` |
| `auto_memory` | 工具自己写的记忆文件 | Claude `MEMORY.md` + 主题文件、WorkBuddy `*_memory.md` |
| `session_distill` | 从会话蒸馏出的结构化记忆 | Codex `memories_1.sqlite`、Trae `session_memory_*.jsonl` |
| `profile` | 用户画像 | Trae `user_profile.md` |
| `doc` | 用户提供的文档 | 手工 `--file` |

### 7.2 需要记录"链中的位置"（待拍板，ADR-011）

Codex 是"近的覆盖远的"，Claude 是"层级拼接 + local 在后"，Cursor 是"按 globs/描述条件加载"。三种都是**有序**的。只存 `source_path` 会丢掉"哪条生效"。

建议给 `MemoryEntry` 增加（**未实现，等拍板**）：

- `precedence: int` —— 在同一来源链中的位置，越大越靠近用户
- `layer: str` —— `global` / `project` / `local` / `nested`

### 7.3 "Markdown 是唯一真相源"只在**输出**侧成立

本仓库现有硬约束（AGENTS.md 第 6 条、ADR-006）说的是：**vault 里的一切以 Markdown 为准，索引可重建**。这条继续成立。

但**输入侧是异构的**：md / jsonl / zstd-jsonl / sqlite。适配器必须允许"从一个非 Markdown 源读出结构 → 落成 Markdown 条目"，而不是假设输入就是 md。

这条不冲突，但必须写明，否则 P2-03 的接口会被"只解析 md"绑死。

---

## 8. 未验证清单（不得据此写代码）

| # | 项 | 状态 |
|---|---|---|
| 1 | Claude Code 的 `<project>` 目录命名规则 | 只有官方文字描述，本机**未安装**（无 `~/.claude`），无实例 |
| 2 | WorkBuddy 的 `<uuid>` 是用户还是工作区 | uuid 且与 `.workbuddy/<uuid>/` 同名，疑似工作区；无官方文档 |
| 3 | WorkBuddy 是否还有别的记忆载体 | 未全盘搜索 |
| 4 | Codex `project_doc_max_bytes` 等默认值 | 来自镜像，非官方页 |
| 5 | Codex `memories_1.sqlite` 的实际行数据形状 | 本机 0 行 |
| 6 | Trae 国际版是否同布局 | 只看 CN 版 |
| 7 | Cursor User Rules / Team Rules 是否落盘 | 官方只说是环境级/面板级 |

## 9. 怎么复核（复制即用）

```powershell
# 1. 哪些 agent 目录真的存在
Get-ChildItem $env:USERPROFILE -Directory -Force |
  Where-Object Name -match '^\.(codex|claude|cursor|trae|workbuddy)' |
  Select-Object FullName

# 2. Codex 记忆库的表结构（只读打开，不写）
python -c "import sqlite3,os,pathlib;p=pathlib.Path(os.environ['USERPROFILE'])/'.codex'/'memories_1.sqlite';c=sqlite3.connect(f'file:{p}?mode=ro',uri=True);print([r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")])"

# 3. Claude Code 是否装了、auto memory 在哪
Test-Path "$env:USERPROFILE\.claude"; Get-ChildItem "$env:USERPROFILE\.claude\projects" -ErrorAction SilentlyContinue

# 4. Trae / WorkBuddy 的记忆文件清单
Get-ChildItem "$env:TRAE_HOME\memory","$env:USERPROFILE\.trae-cn\memory","$env:USERPROFILE\.workbuddy\memory" -Recurse -File -ErrorAction SilentlyContinue
```

## 10. 复审时点

- **P2-04 开工前**：重跑 §9 的四条命令，确认路径与格式没变。
- **每次上游大版本更新后**：至少复核 Codex 的 `AGENTS.md` 链规则与 Claude 的 memory 目录结构（这两家变化最快）。
- 新增来源时：先在本文件加一节，写清置信度，再动 `adapters/` 代码。
