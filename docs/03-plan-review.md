# 策划书评审结论对照表

用途：**防止后续接手的人（或 AI）照着策划书把已经否决的功能又做回来。**

读法：左列是策划书原方案，右列是当前决定与依据。冲突时以 ADR 为准。

## 一、被否决的方案

| # | 策划书位置 | 原方案 | 当前决定 | 依据 |
|---|---|---|---|---|
| 1 | 3.2 / 8.1 分发 | 中央库 → 各工具原生记忆文件**双向写回** | **不做**。只读采集 + 单向只读快照（`exports/`） | ADR-002 |
| 2 | 6.1 目录树 | `vault/` 放在项目仓根目录内 | 代码与数据物理分离，vault 独立私有仓 | ADR-007 |
| 3 | 11.1 备份 | 直接推 GitHub 私有仓 | 先做 U 盘/移动硬盘裸仓冷备；上云推迟到敏感过滤完成 | ADR-007、`docs/00-positioning.md` |
| 4 | 8.1 审核 | 所有提案逐条人工批准 | 三级信任 `auto` / `review` / `blocked` | ADR-003 |
| 5 | 8.4 管理界面 | V0.1 就要 8 个 Web 页面 | 阶段 1-3 只做 CLI | ADR-004 |

## 二、被推迟的方案

| # | 策划书位置 | 原方案 | 当前决定 | 复审时点 |
|---|---|---|---|---|
| 6 | 10 技术选型 | Pydantic | 阶段 1 用 `dataclass` + 手写 `validate()` | 阶段 3 校验 AI 输出时 |
| 7 | 10 技术选型 | Typer | 阶段 1 用 `argparse` | 阶段 4 命令数增长后 |
| 8 | 6.2 元数据 | YAML Frontmatter（完整 YAML） | 阶段 1 自研受限解析器（扁平 key + 内联列表） | **阶段 2 第一件事**，见 P2-01 |
| 9 | 10 数据库 | SQLite（FTS5） | 阶段 1 目录扫描 | 阶段 2，见 P2-02 |
| 10 | 5.3 接口 | Streamable HTTP | 仅 MCP stdio | 阶段 5 之后 |
| 11 | 9.1 平台 | macOS / Linux | 仅 Windows 首发 | V0.2 |

## 三、被采纳并已落地的方案

| # | 策划书位置 | 方案 | 落地位置 |
|---|---|---|---|
| 12 | 6.2 | 每条记忆有稳定 ID、来源、状态、敏感级 | `models.py` |
| 13 | 6.2 | `status` 四态 pending/approved/rejected/archived | `models.py` |
| 14 | 10.2 | 原子写入 + 文件锁 + 单一 Core 写入入口 | `storage/markdown_store.py` |
| 15 | 10.2 | 索引可重建，Markdown 是唯一真相源 | 设计约束，阶段 2 验证 |
| 16 | 11.2 | 外部记忆文本视为不可信数据 | `AGENTS.md` 硬规则 |
| 17 | 13 风险表 | 中文全文搜索需专门分词方案 | 阶段 2 用 FTS5 `trigram`，见 P2-02 |

## 四、被加强的方案

| # | 策划书位置 | 原方案 | 强化后 | 依据 |
|---|---|---|---|---|
| 18 | 7.4 冲突处理 | 展示差异，交用户判断（无时效模型） | 引入 `valid_from` / `valid_until` / `supersedes` / `superseded_by`，旧结论封窗归档而非删除 | ADR-006 |
| 19 | 7.3 防同步循环 | 需要来源标记 + 目标排除 + 哈希三重防护 | 因不做回写（ADR-002），该模块降级为"识别自身 `exports/` 输出"，工作量大幅下降 | ADR-002 |
| 20 | 4.2 适配边界 | 自动发现 + 用户确认路径 + 只读扫描 + 能力声明 | 增加 `source_kind` 字段区分 instruction / memory / transcript / doc | ADR-001、`models.py` |

## 五、策划书未提、但已补入的设计

| # | 设计 | 理由 |
|---|---|---|
| 21 | `source_kind: instruction` | `AGENTS.md` / `CLAUDE.md` 是**给 AI 的指令**，不是用户记忆。不区分会导致"请用中文回复"这类内容被当成人格记忆同步 | 
| 22 | `inbox/rejected/` 持久保留 | 拒绝过的提案不能被下一轮扫描无条件重新导入，否则用户要反复拒绝同一件事 |
| 23 | 记忆 ID 与文件路径解耦 | 移动文件不改变 ID，否则取代链会断 |
| 24 | `extra` 字段透传未知 frontmatter | 前向兼容：未来版本写入的字段，旧版本读写不应丢失 |

## 六、已被证据修正的判断（2026-11-24 核实）

依据：[`04-upstream-formats.md`](04-upstream-formats.md)（实盘 + 官方文档，含置信度与未验证清单）。

| # | 原判断 | 位置 | 核实结果 | 影响 |
|---|---|---|---|---|
| 25 | 把 `AGENTS.md` 当作 Codex 的记忆来源；隐含"一个工具 = 一个记忆文件" | 4.2、13.2 | **`AGENTS.md` 是指令，不是记忆**。Codex 的记忆在 `~/.codex/memories_1.sqlite`（`stage1_outputs`，带 `usage_count`）+ `sessions/**/*.jsonl.zst`；Claude 在 `~/.claude/projects/<project>/memory/`；Trae 在 `session_memory_*.jsonl` | P2-04 的完成判据已重写；Codex 记忆层需单独立工单 |
| 26 | 指令是"一个文件"，采集它即可 | 4.2 | 指令是**有序链**：Codex 从 git root 往下、越近越覆盖（32 KiB 截断）；Claude 从 cwd 往上拼接、`local` 在后；Cursor 按 `globs`/`description` 条件加载 | 只存 `source_path` 会丢失"哪条生效"，见 ADR-011 第 2 条 |
| 27 | "YAML Frontmatter" 暗示输入输出同构 | 6.2 | **输出**侧 vault 全部 Markdown；**输入**侧是 md / jsonl / zstd-jsonl / sqlite 异构 | ADR-011 第 3 条；P2-03 接口不得绑死 markdown |
| 28 | `AGENTS.md` 在 Claude 里一定生效 | 4.2 | 默认 `claude-md-or-agents-md`：**有 `CLAUDE.md` 就不读 `AGENTS.md`**（需 ≥ v2.1.277） | 本仓库自身就是 `AGENTS.md` + `CLAUDE.md` 并存，Claude 侧只读后者 |
