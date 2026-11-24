# 架构决策记录（ADR）

记录格式：状态 ｜ 背景 ｜ 决策 ｜ 理由 ｜ 代价 ｜ 复审时点。
决策一旦「已采纳」，改动前必须先在本文件追加新 ADR 并标注取代关系。

---

## ADR-001 混合记忆：不替换原生记忆

**状态**：已采纳（2026-11-24）

**背景**：可以有两种做法——让用户关闭各工具的原生记忆，统一由 MemoryHub 供给；或保留原生记忆，MemoryHub 只做上层治理。

**决策**：保留原生记忆。MemoryHub 只读采集各来源，中央库作为统一治理层。

**理由**：强行接管原生记忆的失败模式很明确——工具更新后记忆机制变化，用户会同时失去原生能力和中央库。混合模式解耦了"上游变化"和"我方可用性"。

**代价**：数据会重复（同一条偏好在两处存在），必须靠去重和来源标记控制噪音。

**复审时点**：阶段 2 采集适配器完成后，评估重复率是否可接受。

---

## ADR-002 不做回写分发（推翻原策划书 V0.1 的核心功能）

**状态**：已采纳（2026-11-24）

**背景**：原策划书第 3.2 节把「中央库 → 外部工具」的双向分发作为三大同步关系之一，并规划了 `exports/codex/`、`exports/claude/` 等目标目录。

**决策**：v0.x **不向任何 AI 工具的原生记忆文件写入内容**。只做两件事：只读采集；生成单向只读快照（`exports/`），由用户自己决定是否引用。

**理由**：

1. 收益低。工具读取 `AGENTS.md` / `CLAUDE.md` 时只需要"能读到"，不需要"被别人格式化写回"。写回并没有多提供任何能力。
2. 风险高。原策划书第 7.3 节自己列出了同步循环、版本震荡、覆盖用户手写内容三类故障，且承认需要来源标记、目标排除、哈希识别三层防护才能压住。这三层防护的成本接近整个采集模块。
3. 污染用户资产。用户手写的一条规则被程序覆盖，是最容易失去信任的失败方式，一次就够。

**代价**：用户如果想在 Codex 里看到中央库的最新结论，必须手动引用 `exports/` 文件，或者（阶段 5 之后）通过 MCP 查询。体验上有一步摩擦。

**复审时点**：阶段 5 MCP 上线后，如果真实使用中 MCP 无法覆盖某个工具，再单独立项评估该工具的写回。

---

## ADR-003 三级信任替代逐条人工审核

**状态**：已采纳（2026-11-24）

**背景**：原策划书要求所有 AI 产出的提案都进入待审核区，人工批准后才写入正式库。

**决策**：按风险分级：

| 级别 | 触发条件 | 行为 |
|---|---|---|
| `auto` | `category` ∈ {preference, knowledge} 且 `sensitivity` = normal | 直接入正式库，靠 Git 可回滚 |
| `review` | `category` ∈ {profile, decision}，或 `confidence` 低 | 进 `inbox/pending`，等人批 |
| `blocked` | `sensitivity` ∈ {sensitive, secret} | 阻断，禁止批准，除非先降级敏感标记 |

规则实现见 `models.py::MemoryEntry.infer_trust`。

**理由**：逐条审核在真实使用中必然崩溃——记忆增长速度高于人工审核速度，队列一积压用户就会绕过工具，工具的约束力归零。而"可回滚"是可以长期维持的：Git 提供了比人工审核更强的兜底，代价是发现问题晚一点。

**代价**：低风险类别可能被错误内容污染，需要用户偶尔查看 `git log`。为此必须在阶段 2 提供"最近变更摘要"，否则回滚能力等于不存在。

**复审时点**：阶段 4 审核流完成后，用真实数据统计 `auto` 条目的错误率。

---

## ADR-004 阶段 1-3 不做 Web UI

**状态**：已采纳（2026-11-24）

**背景**：原策划书规划了 8 个管理页面（总览 / 来源 / 待审 / 记忆库 / 同步 / AI Provider / 备份 / 设置）。

**决策**：阶段 1-3 只提供 CLI。审核用 `list` + `show` + `approve` + `git diff`。Web UI 推迟到阶段 6，且必须以"自用一个月后仍想打开"为前提。

**理由**：UI 是单人项目最大的时间黑洞。8 个页面的信息架构在自用之前无法确定，先做等于先猜。CLI 的 `git diff` 在审核场景下比任何自研界面都强——它已经有成熟的差异高亮、逐行追溯和回滚。

**代价**：外部用户第一次上手会不习惯。缓解方式是 `doctor` + 明确的错误信息，而不是提前做界面。

**复审时点**：阶段 4 结束后，记录一周内用户实际执行了哪些审核动作，据此决定 UI 内容。

---

## ADR-005 阶段 1 零第三方运行时依赖

**状态**：已采纳（2026-11-24）

**背景**：原策划书建议的技术栈是 Pydantic + Typer + Watchdog + SQLite + FastAPI。

**决策**：阶段 1 的 `src/memoryhub/` 只用标准库。具体替换：

| 原方案 | 阶段 1 实现 | 迁移时点 |
|---|---|---|
| Pydantic | `dataclasses` + 手写 `validate()` | 阶段 3（校验 AI 结构化输出时引入） |
| Typer | `argparse` | 阶段 4（命令数量增长后评估） |
| PyYAML | 自研 frontmatter 最小解析器（扁平 key + 内联列表） | 阶段 2（需要 Obsidian 互操作时引入） |
| SQLite | 目录扫描 | 阶段 2（引入 FTS5 + trigram 分词） |

**理由**：零依赖意味着在任意一台装了 Python 3.11 的机器上 clone 即可运行、可测试，不需要先解决依赖安装。这对单人项目的迭代速度是决定性的，也让 CI 极简。阶段 1 的真正目标是验证数据结构，不是验证库选择。

**代价**：frontmatter 只支持受限 YAML 子集（不支持嵌套与多行字符串），手写解析器有维护成本。`.md` 文件目前不能保证被 Obsidian 正确解析——这一点在阶段 2 必须解决，否则"人类可读"这个核心卖点不成立。

**复审时点**：阶段 2 开始时立即复审，特别是 PyYAML 的迁移。

---

## ADR-006 时序字段是差异化核心，不是可选增强

**状态**：已采纳（2026-11-24）

**背景**：原策划书第 7.4 节把冲突处理定义为"展示差异，交用户判断"，没有引入时效模型。

**决策**：`valid_from` / `valid_until` / `supersedes` / `superseded_by` 是**必填语义**，不是可选元数据。旧结论不删除，而是关闭有效期并建立取代链，`status` 转 `archived`。实现见 `VaultStore.stamp_validity`。

**理由**：用户真正的问题不是"我忘了什么"，而是"这条还算数吗"。删除式更新会永久丢失"当时为什么这么定"，而这恰恰是审计价值所在。选择在 Markdown frontmatter 里实现而不用图数据库，是为了保住 git diff 的可读性——这是与 Zep/Graphiti 的关键分野。

**代价**：查询时必须过滤 `valid_until`，检索逻辑比"读最新"复杂；条目数量会随时间单调增长，需要归档策略。

**复审时点**：阶段 3 冲突检测完成后，验证取代链在真实数据上是否会被正确维护。

---

## ADR-007 代码与数据物理分离

**状态**：已采纳（2026-11-24）

**背景**：原策划书第 6.1 节的目录树把 `vault/` 放在项目根目录内。

**决策**：代码在 `D:/project/MemoryHub`（可开源），数据在 `D:/my-memory`（独立私有仓）。vault 路径通过配置 / 环境变量 / CLI 参数三级解析，任何情况下不写死。

**理由**：

1. 混在一起的第一个后果是 `.gitignore` 越写越长，第二个后果是某次 `git add -A` 把私人记忆提交进公开仓。
2. 采集监控、Git 备份、开源发布三件事作用对象完全不同，共用目录会让每一件都变脏。
3. vault 若落在被采集的来源目录内，会直接触发同步循环。

**代价**：用户需要理解"两个目录"的概念，`init` 必须解释清楚。

**复审时点**：不复审，这是硬约束。

---

## ADR-008 路径与命名规范

**状态**：已采纳（2026-11-24）

**决策**：

- 代码：`D:/project/MemoryHub`（首字母大写，与 GitHub 仓名一致）
- 数据：`D:/my-memory`（全小写、中性命，不随工具改名而失效）
- 两处均为纯 ASCII、无空格。

**理由**：Watchdog 文件事件、PyInstaller 打包、subprocess 调用 Git 三者对空格与中文路径的处理均不稳定。数据目录名与工具名解耦，是因为工具名本身就是暂定的。

**代价**：无。

**复审时点**：不复审。

---

## ADR-009 阶段 1.5 加固：写不丢、读不藏、门禁可判定

**状态**：已采纳（2026-11-24）

**背景**：阶段 1 骨架通过评审后发现三处会在**正常使用路径**上丢数据或失联，且验收门禁本身自相矛盾：

1. `new_id()` 用「秒级时间戳 + 4 位 hex」＝ 65536 种后缀。实测同一秒内生成 2000 个 id 出现 32 个重复；`save()` 直接原子覆盖，重复 id 会**静默覆盖**前一条。批量导入（阶段 2 适配器的 `read() -> list[MemoryEntry]`）正是这个模式。
2. frontmatter 解析失败时 `iter_entries()` 只往 stderr 打一行警告就跳过该条；用户在 Obsidian 里把 `tags: []` 改成块列表 `tags:\n  - dev`，这条记忆就会从 `list`/`search` 里**无声消失**，`show` 还会抛 traceback。这与「Markdown 人类可读、可手改」的定位直接冲突。
3. `doctor` 把「config 未生成」「vault 未 git init」也算 FAIL，而 `init --no-git` 是文档化选项 —— 四处文档写的「必须无 FAIL」在合法路径上不可达。

**决策**：

| 编号 | 决策 | 落点 |
|---|---|---|
| 1 | id 后缀扩到 8 位 hex，且新增 `VaultStore.create()`：id 已被占用则抛 `IdCollisionError`，绝不覆盖 | `models.py::new_id`、`markdown_store.py::create` |
| 2 | `save()` 保留 upsert 语义（状态流转需要覆盖同一 id），但加一层 `_refuse_foreign_owner` 兜底：同一 id 的 `created_at` 不同即判定为他人条目，拒绝写入 | `markdown_store.py::save` |
| 3 | 读失败一律显式上报：`scan()` 返回 `(entries, errors)`，`list`/`search` 打一行汇总警告，`stats` 输出 `unreadable`，`doctor` 判 FAIL | `markdown_store.scan`、`cli.py` |
| 4 | `doctor` 分三级：`FAIL` 阻断门禁（Python 版本、vault 缺失、layout 缺失、条目不可读）/ `WARN` 不阻断（config 未生成、git 未初始化、vault 位于 OneDrive） | `cli.py::cmd_doctor` |
| 5 | 补齐 ADR-003 明文规定但未实现的规则：`confidence == low` 归入 `review` | `models.py::infer_trust` |
| 6 | 查询默认过滤 `valid_until` 已过期的条目（ADR-006 的「查询时必须过滤」），历史视图用 `--include-expired` 显式打开；显式 `--status` 视为历史查询，不再二次过滤 | `models.is_expired`、`cli.py` |
| 7 | `--approve` 遇到 `trust=review` 必须拒绝，需显式 `--force` 才能越过；`blocked` 仍不可越过 | `cli.py::cmd_new` |
| 8 | `--vault` 在子命令前后都接受，任一缺省不再覆盖已解析的值 | `cli.py::_vault_parent` |

**理由**：这 8 条里没有一条是功能，全部是「让已有的承诺真的成立」。第 1、2 条防的是静默数据丢失 —— 一次丢数据就够让用户不再信任这个库；第 3、4 条防的是「工具说没事，实际有事」，它比报错危险得多；第 5、7 条是把 ADR-003 从文档变成代码，否则「分级信任」这个核心差异点在任何一次 `--approve` 面前都不存在。

**代价**：

- id 变长一点，旧 vault 的 4 位 id 继续可读（正则接受 4–32 位），但不会自动迁移。
- `scan()` 会把整库读进内存，大于数万条时应换成流式统计（阶段 2 索引上线后此路径不再是主路径）。
- 默认过滤过期条目改变了 `list`/`search` 的返回集，这是行为变更，需在 README 中写明。
- `--approve` 变严格后，原有的批量脚本若依赖它直接入库会失败 —— 这是有意的。

**复审时点**：阶段 2 采集适配器落地后复审第 1、2 条（批量导入是碰撞的唯一真实场景）。

---

## ADR-010 未决：`project` 分类的信任归属

**状态**：待所有者拍板（2026-11-24 记录，未生效）

**问题**：ADR-003 的信任表只覆盖了 `preference`/`knowledge`（→`auto`）与 `profile`/`decision`（→`review`），`CATEGORIES` 里的 **`project` 没有归属**。当前实现让它落 `auto`，等于由代码替所有者做了一个决策。

**待定选项**：

1. `project` → `review`：项目背景会改变 AI 的行为，与 `profile` 同级。
2. `project` → `auto`：承认现状，但要在 ADR-003 里补上这一行。

**在拍板之前**：代码保持 `auto`（不擅自改决定），`tests/test_store.py::test_decision_category_needs_review` 不动，本 ADR 记录该缺口。

---

## ADR-011 输入是异构的：格式基线 + 两个待定的字段扩展

**状态**：待所有者拍板（2026-11-24 记录，未生效）

**背景**：核实上游来源后（证据与置信度见 [`04-upstream-formats.md`](04-upstream-formats.md)），三件事与现有设计不符：

1. `AGENTS.md` 是**指令**，不是记忆。Codex 的记忆在 `~/.codex/memories_1.sqlite`（表 `stage1_outputs`，带 `usage_count` / `last_usage`），会话原文在 `sessions/**/rollout-*.jsonl.zst`；Claude Code 的记忆在 `~/.claude/projects/<project>/memory/`（`MEMORY.md` 索引 + 主题文件，frontmatter 带 `type` 与 `modified`）；Trae 的记忆是 `session_memory_*.jsonl`（`intent` / `actions` / `outcome` / `learned`）。策划书 §4.2 把 `AGENTS.md` 当作 Codex 的记忆来源，这个判断是错的。
2. 指令是**有序链**，不是单个文件。Codex 从 git root 往下走、越近越靠后覆盖前面的（并有 32 KiB 截断）；Claude 从 cwd 往上拼接、同目录 `CLAUDE.local.md` 排在后；Cursor 用 `globs` / `description` 条件加载。只存 `source_path` 表达不了"哪条生效"。
3. `source_kind` 的 `memory` 值太笼统，无法区分"工具自动写的记忆"与"从会话蒸馏的结构化记忆"。

**待定决策（三条，需分别点头）**：

| # | 建议 | 若不采纳的后果 |
|---|---|---|
| 1 | `source_kind` 扩展为 `instruction` / `auto_memory` / `session_distill` / `profile` / `doc`（`memory` 保留为兼容值） | 冲突裁决无法区分"指令冲突"与"记忆冲突"，`auto` 信任级的适用范围说不清 |
| 2 | `MemoryEntry` 增加 `precedence: int` 与 `layer: str`（`global` / `project` / `local` / `nested`） | Codex 的覆盖语义、Claude 的加载顺序在入库时丢失，事后无法还原 |
| 3 | 明确写入：**"Markdown 是唯一真相源"仅指输出侧（vault）**，输入侧允许 md / jsonl / zstd-jsonl / sqlite | P2-03 的接口会被"只解析 md"绑死，写第二遍是必然 |

**为什么这三条要一起拍**：它们共同决定 `MemoryEntry` 的字段集。分开改会连续破坏 `from_markdown` / `to_meta` 的稳定性，而字段语义是本项目明确要求"宁可不写功能也不要改"的东西。

**代价**：

- 采纳第 2 条要动 `models.py` 的字段集与 `_KNOWN_KEYS`，并补 `extra` 之外的兼容测试；已冻结的"字段已冻结"声明需要一份新的 ADR 修订。
- 第 3 条需要在 `AGENTS.md` 第 6 条上加一句限定，属于改契约文件，**只能由所有者动**。

**在拍板之前**：`models.py` 不动。适配器开发不启动（避免基于错的形状写两遍）。P2-01（PyYAML）可以照常做，它与此无关。

**复审时点**：P2-03 开工前必须拍板；`docs/04-upstream-formats.md` 每次上游大版本更新后复核。
