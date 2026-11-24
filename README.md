# MemoryHub

跨 AI 工具的个人记忆治理层。本地优先、可审计、可迁移。

> **状态：阶段 1 骨架，尚未发布。** 目前只有记忆模型、存储层、CLI 基础命令和测试；
> 采集适配器、AI 整理、审核界面、MCP 服务均未实现。请不要把它当作可用工具使用。

---

## 要解决的问题

你同时用 Codex、Claude Code、Cursor 和几个自建 Agent。每个工具各记一份你的偏好、项目背景和技术决策。结果是：

1. **重复交代** —— 每个新会话都要重新解释自己是谁、在做什么。
2. **结论过期** —— 三个工具各记一版，没人知道哪条还有效。
3. **不可迁移** —— 想换工具，记忆带不走；想回滚，不知道改过什么。

MemoryHub 不替换任何工具的原生记忆，而是在它们之上加一层**治理**：采集、裁决、留痕、备份、检索。

## 与现有方案的区别

| 方案 | 它解决的 | 它没解决的 |
|---|---|---|
| Basic Memory（Markdown + SQLite + MCP，AGPL-3.0） | 存与查，人类和 AI 共读同一批 Markdown | 没有多来源治理：谁来判"这条已经过期"，它不管 |
| mem0 / OpenMemory | 语义记忆抽取与检索，跨客户端召回 | 面向应用开发者的记忆 API，不是个人记忆资产 |
| Zep / Graphiti | 事实的时间有效性（双时态） | 需要图数据库栈，正文不再是可直接读的 Markdown |
| Letta（MemGPT） | Agent 运行时内的记忆块 | 绑定在 Letta 的运行时里，不是跨工具层 |
| ChatGPT / Claude 内置记忆 | 单产品内自动记忆 | 跨产品完全不通，也不暴露给其他工具 |

MemoryHub 的着力点是**其余方案都空着的那一格：治理**。具体是三件事：

- **来源留痕**：每条记忆能追到哪个工具、哪个文件、哪次哈希。
- **时效裁决**：`valid_from` / `valid_until` / `supersedes`，旧结论不删除而是封窗，可查"当时为什么这么定"。
- **分级信任**：不是所有记忆都配得上人工逐条审核，但所有记忆都必须可回滚。

（竞品信息基于 2026 年 11 月的公开资料，正式对外表述前需再次核实版本与能力边界。）

## 当前已实现

- `MemoryEntry` 领域模型：分类、状态、信任级、敏感级、来源、时效字段
- 零依赖 frontmatter 读写（受限 YAML 子集）
- 原子写入 + 跨进程文件锁；一个 id 只对应一个文件
- `inbox`（待审）与 `vault`（正式）之间的流转，不留重复文件
- 三级信任策略：`auto` / `review` / `blocked`，敏感条目禁止直接批准
- CLI：`init` `doctor` `new` `list` `show` `search` `approve` `reject` `stats`
- 15 个单元测试，全部通过

## 快速开始

```bash
# 1. 创建记忆库（独立于本仓库，默认 D:/my-memory）
PYTHONPATH=src python -m memoryhub init --vault D:/my-memory

# 2. 环境自检
PYTHONPATH=src python -m memoryhub doctor

# 3. 写入一条提案（默认 pending，不会直接进正式库）
PYTHONPATH=src python -m memoryhub new \
  --category preference \
  --body "用户开发项目时优先考虑 Python；除非有明确性能需求，否则不选 Rust。" \
  --tag development --adapter codex --kind instruction

# 4. 审核
PYTHONPATH=src python -m memoryhub list --status pending
PYTHONPATH=src python -m memoryhub approve mem_xxxxxxxx_xxxx

# 5. 跑测试
PYTHONPATH=src python -m unittest discover -s tests -v
```

安装为命令（需要 hatchling）：

```bash
pip install -e .
memoryhub doctor
```

## 目录结构

```
D:/project/MemoryHub        代码（本仓库，可开源）
  src/memoryhub/            实现
  tests/                    单元测试
  docs/                     定位、决策、路线、策划书
  tools/                    一次性转换脚本
D:/my-memory                你的记忆（独立私有仓，绝不进本仓库）
  vault/                    正式记忆，按分类分目录
  inbox/pending/            待审提案
  inbox/rejected/           拒绝记录（保留，避免反复导入）
  inbox/conflicts/          冲突待裁决
  sources/                  原始来源快照
  exports/                  对外导出的只读快照
  logs/                     运行日志
```

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 记忆模型、存储层、原子写入、CLI、测试 | 骨架完成 |
| 2 | 采集适配器（Codex / Claude Code）、文件监控、哈希去重、SQLite FTS5 索引（trigram） | 未开始 |
| 3 | AI 整理（仅产出提案）、冲突检测、Pydantic 校验 | 未开始 |
| 4 | 审核流 + 差异预览（CLI） | 未开始 |
| 5 | MCP stdio（只读 + propose）、Git 自动备份 | 未开始 |
| 6 | 打包（PyInstaller）、开机常驻、基础 Web UI | 未开始 |

## 明确不做

- 不向任何 AI 工具的原生记忆文件**回写**内容。只读采集 + 单向只读快照。
- 不训练、不托管模型。AI API 完全可选，不配置也能完成采集、审核、分发、备份。
- 不在 v0.x 建立云端账户、SaaS 或自建同步服务器。
- 不把 GitHub 当实时同步总线。

## 许可证

待定（倾向 Apache-2.0：含专利授权，便于被其他工具集成）。在确定之前请勿分发。

## 文档索引

| 文件 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 工程契约：7 条不可推翻的决策、编码约定、明确禁止。**AI Agent 必读** |
| [`HANDOFF.md`](HANDOFF.md) | 现状交接：做到哪、怎么跑、已知问题、下一步 |
| [`docs/00-positioning.md`](docs/00-positioning.md) | 定位与竞品差异 |
| [`docs/01-decisions.md`](docs/01-decisions.md) | 8 条 ADR，含每条决策的代价与复审时点 |
| [`docs/02-roadmap.md`](docs/02-roadmap.md) | 阶段拆解与可领工单 |
| [`docs/03-plan-review.md`](docs/03-plan-review.md) | 策划书 ↔ 当前决定的对照表（防止做回已否决功能） |
| [`docs/reference/`](docs/reference/) | 项目策划书原文与可读版，只读 |

用 Claude Code / Cursor 接手时，`CLAUDE.md` 与 `.cursor/rules/project.mdc` 会指向上面的入口。
