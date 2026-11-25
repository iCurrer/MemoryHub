# AGENTS.md

本文件是本仓库的工程契约。任何 AI 编程 Agent（Codex / Claude Code / Cursor 等）在修改本仓库前必须先读完本文；与本文冲突的改动一律不做，先提出。

## 一句话定位

MemoryHub 是**跨 AI 工具的个人记忆治理层**：采集各 AI 工具的本地记忆与指令文件，做去重、冲突裁决、来源留痕与版本备份，再通过 MCP 提供给任意 AI 客户端检索。

它**不是**聊天机器人，**不是**又一个 Markdown 笔记库，**不是**向量记忆服务。

## 当前阶段

阶段 1 / 1.5 / 2（除 P2-03b）/ 3（部分：P3-01、P3-02、P3-05 完成；P3-03 仅拆分与分类，摘要未做）/ 4 / 5（含 P5-06 回写分发）/ P6-03（本机页面，提前落地）已完成：数据层、三个只读采集适配器、轮询监控、FTS5 索引、CLI 审核流、MCP（含权限分级）、敏感检测、Git 备份、AI 层（用户自带 API key）、冲突检测与取代链、受管区块回写、两个队列的本地页面。
阶段 3 剩余部分（P3-00 / P3-04）与阶段 6 的打包/托盘尚未实现，**不得在 README 或文档中声称已实现**。实时进度以 `HANDOFF.md` 为准。

> **冲突裁决的边界不可被绕过**：模型只出提案（`inbox/conflicts/*.json`），`supersedes` / `valid_until` 只由 `memoryhub supersede` 写，且必须由人给两个 id。任何"检测到冲突就自动归档"的实现都违反 ADR-003 与 ADR-018。

> **AI 层的定位不可被误解**：`ai/` 只负责「读内容并给出结构化提案」，产出恒为 `status: pending`，且每条必须带从原文逐字复制的 `quote`（定位不到即标记待核实）。**找文件仍由算法做**（`Adapter.discover()`），因为那是确定的；读内容交给模型，因为那是语义的。不要把 `quote` 校验去掉，它是唯一能自动化的防幻觉手段。见 ADR-016。

## 上手顺序（AI Agent 必读）

1. **本文件** —— 契约优先于一切
2. [`HANDOFF.md`](HANDOFF.md) —— 做到哪一步、怎么跑、已知问题、下一步
3. [`docs/02-roadmap.md`](docs/02-roadmap.md) —— 领一个工单，按编号顺序执行
4. [`docs/01-decisions.md`](docs/01-decisions.md) —— 想推翻决策前先看代价与复审时点
5. [`docs/03-plan-review.md`](docs/03-plan-review.md) —— 策划书里哪些方案已被否决

**文档地图**

| 文件 | 作用 | 谁维护 |
|---|---|---|
| `AGENTS.md` | 工程契约，规则唯一来源 | 项目所有者 |
| `HANDOFF.md` | 现状交接，每次提交后更新 | 当次改动人 |
| `CLAUDE.md` / `.cursor/rules/` | 只做导航指针，不重复规则 | 项目所有者 |
| `docs/00-positioning.md` | 定位与竞品差异 | 项目所有者 |
| `docs/01-decisions.md` | ADR 决策记录 | 当次改动人追加 |
| `docs/02-roadmap.md` | 工单与阶段门禁 | 当次改动人勾销 |
| `docs/03-plan-review.md` | 策划书 ↔ 决定的对照表 | 决策变更时更新 |
| `docs/04-upstream-formats.md` | 上游记忆来源的路径/格式基线（实盘 + 官方文档，含置信度） | 新增来源或上游大版本变更时更新 |
| `docs/reference/` | 策划书原文与转换版，**只读** | 不收改动 |

## 已定决策（不可推翻，改了要先写 ADR）

1. **回写只在受管区块内。** ADR-002 仍然禁止适配器写别人的文件（`Adapter.can_write` 恒 `False`）。`memoryhub sync` 是唯一例外：只写 `<!-- MEMORYHUB:BEGIN -->` 与 `<!-- MEMORYHUB:END -->` 之间，区块外一个字节不碰，删掉区块即完全还原；读方必须先剥掉区块再解析，否则同步会成为下一次导入的输入。任何在区块外落笔、或「顺手整理目标文件格式」的实现都违反 ADR-019。
2. **三级信任替代逐条审核。** `auto`（低风险自动入库，靠 Git 可回滚）/ `review`（进 inbox 等人批）/ `blocked`（敏感或冲突，阻断）。见 ADR-003。
3. **界面只做两个队列，且只在本机。** ADR-004 的「先不做 Web UI」已被 ADR-020 部分取代：`memoryhub serve` 只有「待审」与「冲突提案」两个视图（外加只读的回写计划），只绑 `127.0.0.1`、无 `--host`、校验 `Host` 头、每次启动一枚随机 token（走 header 不走 cookie）。写操作必须调 CLI 同一批函数（`store.set_status` / `conflicts.apply_supersede`）——**不得在页面里重写任何规则**；`sync --write` 永远留在命令行。要加第三个视图（编辑正文、全库检索、批量操作），先写 ADR。
4. **代码与数据物理分离，且由机制保证。** 本仓库只装代码，不含 vault、索引、日志、来源快照；vault 路径由配置提供，默认 `D:/my-memory`。**云端只有这一个仓（代码仓）；每个用户的记忆仓各自独立、自选远端，MemoryHub 不代建不代推。** 三道门禁不得删除或绕过：`init` 拒绝把 vault 建在任何 git 工作树内；`doctor` 把 `repo isolation` / `vault remote` 当硬检查；`tools/hooks/` 的 pre-commit 与 commit-msg 扫描暂存内容**与提交信息**。见 ADR-013。
5. **单一 Core API。** 对 **vault** 的所有写操作必须经过 `memoryhub.storage`，Adapter 与 CLI 一律不得直接写 vault 文件。`sync` 写的是 vault 之外的投影，不经 storage，这是 ADR-019 的例外，不构成第二套写入路径。
6. **Markdown 是唯一真相源（仅指输出侧 vault）。** 任何索引、SQLite、缓存都必须可从 vault 的 Markdown 重建；不得出现只能从索引读到的记忆。输入侧（上游来源）允许 md / jsonl / zstd-jsonl / sqlite（ADR-011）。
7. **依赖从严。** 阶段 1 曾零第三方依赖（ADR-005）；现仅允许 `pyyaml`（ADR-005 部分取代）与 `mcp`（ADR-012）。新增任何运行时依赖必须先写 ADR。

## 目录职责

```
src/memoryhub/
  config.py              # 配置读写（TOML）、vault 路径解析
  models.py              # MemoryEntry 领域模型与校验
  frontmatter.py         # frontmatter 读写（PyYAML）
  safety.py              # 敏感分级：凭证 / 强 PII / 弱 PII
  storage/markdown_store.py  # 唯一写入入口：原子写、文件锁、扫描、检索
  storage/index.py       # SQLite FTS5 索引（可从 Markdown 重建）
  adapters/              # 外部来源适配器（只读）：workbuddy / claude / codex
  ai/                    # 用户自带 API key 的整理层：provider / schema / organize / conflict（只产提案）
  managed.py             # 受管区块标记与剥离；写方与读方共用，刻意零依赖
  sync.py                # 回写：把 vault 投影成各工具记忆文件里的受管区块（ADR-019）
  watcher/               # 轮询 + 防抖 + 哈希去重
  mcp/                   # MCP stdio 服务端与权限分级
  backup/                # vault 的 git 备份与变更历史
  cli.py                 # CLI 入口
tests/                   # unittest，需 pyyaml + mcp，必须能在无网络的机器上跑通
docs/                    # 定位与决策记录
```

新增模块时的落位规则：外部工具相关逻辑进 `adapters/`，AI 相关进 `ai/`，Git 相关进 `backup/`，**通用工具函数不得放进 `models.py`**。

## 编码约定

- Python 3.11+，`from __future__ import annotations` 开工。
- 公开函数带类型标注；不用 `Any` 兜底除非确有外部未知结构。
- 文件写入一律 `tmp + os.replace` 原子替换，且必须持锁；禁止 `open(..., "w")` 直接覆盖 vault 内文件。
- 时间一律带时区 ISO 8601（`2026-11-24T13:51:22+08:00`），禁止裸 `datetime.now()`。
- 路径一律 `pathlib.Path`；仓库内部不得出现中文路径或含空格路径的硬编码。
- 异常信息只写 stderr；**stdout 保留给 MCP 协议与 CLI 结果**（MCP stdio 模式下 stdout 被协议独占）。

## 明确禁止

- 禁止引入图数据库、向量库、ORM 作为阶段 1-2 依赖。
- 禁止让 AI 生成的内容直接落盘为 `status: approved`；AI 只能产出 `pending`。
- 禁止把 `AGENTS.md` / `CLAUDE.md` 的整段内容当作"用户人格记忆"入库 —— 它们是**指令**（`source_kind: instruction`），不是记忆。
- 禁止在 vault 内保存 API Key、令牌、密码；敏感字段一律写引用标识。
- 禁止为了"跑通"而吞掉异常或静默降级；失败要显式报错。
- 禁止在未经确认的情况下改写已定决策、迁移目录结构、新增依赖。
- 禁止提交与真实身份绑定的一致性信息：住址、手机号、身份证号、学校/单位名、本机 profile UUID、含用户名的绝对路径。测试需要这类形状时用明显虚构的值，并在该行加 `# pii:synthetic`。
- 禁止把 `serve` 绑到 `0.0.0.0` 或任何非环回地址，也禁止给它加 `--host` 参数、把 token 放进 cookie 或 localStorage、或去掉 `Host` 头校验。一个能批准记忆的页面只该在本机可达。
- 禁止在页面里重写审核或取代的规则，或让页面直接改动 vault 文件。它只能调 CLI 用的那批函数（ADR-020）。
- 禁止把 vault 路径指向本仓库内部，或给 vault 配置指向代码仓的远端——即使只是"临时测试"。
- **提交信息视同发布内容。** 它会出现在公开页面上，且历史改写无法从 GitHub 彻底删除。

## 验收方式

```bash
sh tools/setup_dev.sh                              # 首次 clone 后跑一次，启用边界钩子
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m memoryhub doctor
python tools/lint_staged.py --all                  # 边界与身份信息全量扫描
```

两项都通过才算完成一次改动。新增功能必须补测试；只加功能不加测试的提交视为未完成。
