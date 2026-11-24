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
| Claude Code（CLI） | `~/.claude/CLAUDE.md`、`./CLAUDE.md`、`./CLAUDE.local.md`、`.claude/rules/*.md` | `~/.claude/projects/<project>/memory/`（`MEMORY.md` + 主题文件） | Markdown（记忆带 YAML frontmatter） | 官方 |
| Claude Desktop（应用） | 未见落盘文件 | 运行期由系统提示定义；本机**未找到** `MEMORY.md` | Markdown（`MEMORY.md` + 主题文件，同 Code） | 实盘（目录）+ 实盘技能（格式） |
| Cursor | `.cursor/rules/*.mdc`（**只认 `.mdc`**）、用户级 / 团队级规则、`AGENTS.md` | 无独立记忆层 | Markdown + frontmatter | 官方 |
| WorkBuddy | 未见（未验证） | 三种形态：`memory/<uuid>_memory.md`、`user-<uuid>-personal/`、`<workspace>/.workbuddy/memory/` | Markdown，**无 frontmatter** | 实盘 |
| Trae CN | `~/.trae-cn/skills/`（未验证是否算指令层） | `~/.trae-cn/memory/` 三层 | Markdown + **JSONL** | 实盘 |

**一句话**：`AGENTS.md` 是**指令**，不是记忆。把两者混为一谈，是本项目最容易犯的第二个设计错误（第一个是回写分发，见 ADR-002）。

> **注意 "Claude" 是两个产品**：Claude **Desktop**（Electron 应用，`%LOCALAPPDATA%\Claude-Data\`）与 Claude **Code**（CLI，`~/.claude` + `claude` 命令）。本机只装了前者。把两者混为一谈会导致适配器扫错目录、静默 0 条 —— 详见 §3.0。

---

## 2. Codex

### 2.1 指令层是一条带覆盖语义的链（**实盘已验**）

来源：官方页的镜像副本 + **本机 codex-cli 0.147.0 实测**（`learn.chatgpt.com` 直接抓取失败）。

> **实测（2026-11-24）**：搭三层目录树 —— 根 `AGENTS.md`（ROOT-RULE→APPLE）、
> `sub/AGENTS.md`（SUB-RULE→BANANA）、`sub/AGENTS.override.md`（OVERRIDE-RULE→CHERRY），
> 在 `sub` 下执行 `codex exec` 让它复述指令行：
>
> ```
> ROOT-RULE 1 次 | OVERRIDE-RULE 1 次 | SUB-RULE 0 次
> APPLE     1 次 | CHERRY        1 次 | BANANA   0 次
> ```
>
> 结论：根与嵌套**都加载**；同目录下 `AGENTS.override.md` 生效时 `AGENTS.md`
> **完全不被读取**（是遮蔽，不是合并）。已固化为 `adapters/codex.py` 的测试。

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

### 3.0 先分清：Claude Desktop 与 Claude Code 是两个产品（实盘）

| | Claude Desktop（应用） | Claude Code（CLI） |
|---|---|---|
| 本机装了吗 | **装了** | **没装** |
| 证据 | `%LOCALAPPDATA%\Claude-Data\`（67 MB，Electron：`blob_storage`/`IndexedDB`/`Local Storage`），其中含 `claude-code-sessions/` 与 `local-agent-mode-sessions/` | 无 `~/.claude`，`where claude` 无结果，注册表无卸载项 |
| 读 `CLAUDE.md` 吗 | 未见落盘指令文件 | 是（§3.1） |
| 记忆在哪 | 运行期由系统提示定义；本机**没有找到** `MEMORY.md` | `~/.claude/projects/<project>/memory/`（§3.3） |

**Desktop 应用自带两个与记忆直接相关的技能**（实盘，随应用下发，路径
`local-agent-mode-sessions\skills-plugin\...\skills\`）：

- `consolidate-memory` — 描述是 `Reflective pass over your memory files — merge duplicates, fix stale facts, prune the index`
- `import-memory` — `Import a memory export from another AI assistant into Claude's memory`

`consolidate-memory` 的正文（实盘读到的原文）确认了它的记忆结构：

- 一个**记忆目录** + **`MEMORY.md` 索引** + **主题文件**；
- 索引格式：`- [Title](file.md) — one-line hook`，上限 **200 行 / ~25KB**，一行一条；
- 记忆分**durable**（偏好、工作风格、关键关系、重复流程）与 **dated**（具体项目、截止日期、一次性任务），过期的要 retire 或把结论折进 durable；
- 要求把 "next week / this quarter" **换成绝对日期**。

> **战略含义（重要）**：Anthropic 已把"去重 / 修过期 / 剪索引"做成**提示词技能**，靠模型自觉执行，没有数据层、没有来源留痕、没有可审计的裁决记录。这既验证了 MemoryHub 的定位，也是最直接的同质化风险：如果用户认为"让模型整理一下"就够了，就不会为本工具付费或自用。**我们的差别必须落在"可审计 + 可回滚 + 跨工具"上，而不是"我也会整理"。**

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

> **实现状态**：`adapters/claude.py` 已按本节实现（P2-05）。但**本机未安装 Claude Code，未经真实样本验证** —— 首次在真机使用前请重跑 §9，并复核 §8 第 1 项。

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

**三种形态，别只看到一个。** 最早只发现了第 1 种，第 2 种才是真正描述「这个人」的：

| # | 路径 | 内容 | 已适配 |
|---|---|---|---|
| 1 | `~/.workbuddy/memory/<profile-uuid>_memory.md`（+ `.bak`） | `# User Memory Profile` + 块引用元数据 + bullet 列表 | ✅ `auto_memory` |
| 2 | `~/.workbuddy/user-<uuid>-personal/` | `IDENTITY.md` / `SOUL.md` / `USER.md` / `MEMORY.md` / `workspace-state.json` | ✅ 前三个 `profile`，`MEMORY.md` 按 bullet 切 |
| 3 | `<workspace>/.workbuddy/memory/` | `MEMORY.md`（按标题组织的长期事实）+ 按天文件 `YYYY-MM-DD.md` | ❌ **未实现**（P2-03b） |

> 公开仓脱敏：真实 `<profile-uuid>` 已替换为占位符，它是本机 WorkBuddy 配置目录的稳定标识符，不应随仓库公开。

### 5.1 `memory/<uuid>_memory.md`

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

### 5.2 `user-<uuid>-personal/`（人格目录）

- `IDENTITY.md`（683 B）/ `SOUL.md`（1.8 KB）/ `USER.md`（2.4 KB）：标题 + 段落，描述身份、风格、偏好。
- `MEMORY.md`（1.7 KB / 26 行 / 5 个标题 / 17 条 bullet）：标题是 `关于这个人的事实` / `长期记忆` / `硬约束` / `元规则`，**没有 markdown 链接**，所以它不是"指针索引"，而是**按标题分组的记忆正文**。
- 与 §3.0 里 Claude 的 `MEMORY.md` 不同：Claude 的是链接索引（`- [Title](file.md)`），WorkBuddy 这个不是。**不要用同一套解析器想当然处理两者。**

### 5.3 `<workspace>/.workbuddy/memory/`（未实现）

- `MEMORY.md`（6 KB / 90 行 / 5 个标题 / 29 条 bullet）：项目级长期事实，标题如 `Notion 是唯一事实源`、`字段约定`、`专家分工`。
- 同目录的 `2026-08-31.md`（5.6 KB）、`2026-09-01.md`（1.7 KB）：**按天**的会话沉淀。

未实现的原因：工作区路径不是从 home 推出来的，必须由用户给出根目录（或从 `workspace-state.json` 读，尚未验证其结构）。见 P2-03b。

> 未验证：`<uuid>` 代表用户还是工作区；`workspace-state.json` 是否含工作区路径列表。

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
| 1 | Claude Code 的 `<project>` 目录命名规则 | 官方只有文字描述；本机**未安装 Claude Code**（无 `~/.claude`），无实例 |
| 2 | WorkBuddy 的 `<uuid>` 是用户还是工作区 | uuid 且与 `.workbuddy/<uuid>/` 同名，疑似工作区；无官方文档 |
| 3 | WorkBuddy 是否还有别的记忆载体 | 已找到三种（§5），**是否还有第四种未知**；`workspace-state.json`（190 B）结构未验证 |
| 4 | Codex `project_doc_max_bytes` 等默认值 | 来自镜像，非官方页 |
| 5 | Codex `memories_1.sqlite` 的实际行数据形状 | 本机 0 行 |
| 6 | Trae 国际版是否同布局 | 只看 CN 版 |
| 7 | Cursor User Rules / Team Rules 是否落盘 | 官方只说是环境级/面板级 |
| 8 | Claude Desktop 的 auto-memory 目录到底在哪 | 应用装了（`%LOCALAPPDATA%\Claude-Data`），但本机**搜不到任何 `MEMORY.md`**；技能正文说"目录由系统提示定义"，即运行期才知道。可能在 `IndexedDB`/`Local Storage` 或云端 |
| 9 | Claude Desktop 的 MCP 配置写在哪 | `%APPDATA%\Claude\claude_desktop_config.json` **不存在**（README 提到 `%APPDATA%\Claude` 是旧版本残留路径）；新位置未定位 |

## 8.5 候选来源清单（未适配，按"有没有真·本地记忆"分档）

判据不是"这个工具有没有规则文件"，而是**它会不会自己往磁盘写记忆**。

### A. 有真·本地自动记忆（agent 自己写，可直接采集）

| 工具 | 记忆落点 | 格式 | 置信度 | 适配成本 |
|---|---|---|---|---|
| **Gemini CLI** | `~/.gemini/GEMINI.md`（全局偏好）+ 仓库 `GEMINI.md`（项目指令）+ **per-project private memory 目录**（路径官方未写，待核） | Markdown | 官方 | 低 —— 指令层复用 `sections()` |
| Windsurf | "Cascade Memories"（官方承认有记忆，**落盘位置未核实**） | 未核实 | 官方 | 中 |
| Cline / Roo Code | 项目内 `memory-bank/*.md`，由提示词驱动 agent 维护 | Markdown | 官方（最佳实践） | 低，但要用户给项目根 |
| **Cherry Studio** | `Data\Agents\<agent>\`：`USER.md`（对该用户的认知）、`memory\FACT.md`（长期知识）。**`SOUL.md` 是助手自己的性格，不是用户记忆** | Markdown | **实盘** | 已适配（`cherry`），但**默认只采用户自建的 agent**；本机只有内置的 `cherry-support`，故发现 0 条 |
| 已适配 | Codex、Claude Code/Desktop、WorkBuddy、Trae CN | — | — | 见前文各节 |

### B. 只有「指令」没有「记忆」（agent 只读，人写）

Cursor（`.cursor/rules/*.mdc`）、GitHub Copilot（`.github/copilot-instructions.md`）、Continue（`.continue/rules`）、Aider（`CONVENTIONS.md`）、Zed（`.rules`）。
这些值得采，但只能标 `source_kind=instruction`，**不要当人格记忆**（AGENTS.md 的硬规则）。

### C. 本机装了但没有可采记忆

| 工具 | 实盘看到什么 |
|---|---|
| GitHub Copilot CLI | `~/.copilot/`：`config.json` + `session-state/`（会话状态，不是记忆） |
| Tencent CodeBuddy | `~/.codebuddy/`：`diagnostics/`、`logs/`、`skills/`，未见记忆库 |
| DSH（本 harness） | `~/.dsh/`：`profiles/`、`sessions/`、`storages/`、`skills/`（会话，不是记忆） |
| Claude Desktop | 云端（见 §3.0） |

### D. 一个正在成型、但**语义各不相同**的公共形状

WorkBuddy 的 `user-*-personal/{IDENTITY,SOUL,USER,MEMORY}.md`、Cherry Studio 的
`Agents/<agent>/{SOUL,USER,memory/FACT}.md`、Claude 的 `memory/{MEMORY.md, 主题}.md`
—— 文件名几乎一样，**但归属不同**，这是最容易踩的坑：

| 文件 | 属于谁 | 应归为 |
|---|---|---|
| `SOUL.md` / `IDENTITY.md` | **助手自己**的人格与语气（"mirror the user's terminology"） | `instruction`，通常**不该入库** |
| `USER.md` | 助手**对用户的认知** | `auto_memory` + `category=profile` → review |
| `MEMORY.md` / `FACT.md` | 助手积累的事实 | `auto_memory` + `category=knowledge/profile` |
| WorkBuddy 的 `user-*-personal/USER.md` | **用户自己**的身份描述 | `category=profile` |

> **教训（2026-11-24 实际犯过）**：我一开始把 Cherry Studio 的 `SOUL.md` 当成"用户人格"按 `profile` 导入了。它其实是那个助手的性格设定——正好撞上 `AGENTS.md` 的硬规则"指令不是人格记忆"。
>
> 推论：**共用解析器可以，共用分类判断不行。** `adapters/persona.py` 只负责切分格式，`source_kind` / `category` 必须由每个工具自己声明。
>
> 还有一个更实际的结论：**内置助手（`cherry-support`、`system`）的文件是产品模板，不是用户数据。** 默认跳过；跳过之后本机发现 0 条，这才是诚实的答案。

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
