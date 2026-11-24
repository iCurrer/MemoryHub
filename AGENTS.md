# AGENTS.md

本文件是本仓库的工程契约。任何 AI 编程 Agent（Codex / Claude Code / Cursor 等）在修改本仓库前必须先读完本文；与本文冲突的改动一律不做，先提出。

## 一句话定位

MemoryHub 是**跨 AI 工具的个人记忆治理层**：采集各 AI 工具的本地记忆与指令文件，做去重、冲突裁决、来源留痕与版本备份，再通过 MCP 提供给任意 AI 客户端检索。

它**不是**聊天机器人，**不是**又一个 Markdown 笔记库，**不是**向量记忆服务。

## 当前阶段

阶段 1（核心数据层）已完成骨架：记忆模型、frontmatter 读写、原子写入、CLI 基础命令、单元测试。
阶段 2 及以后尚未实现，**不得在 README 或文档中声称已实现**。

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
| `docs/reference/` | 策划书原文与转换版，**只读** | 不收改动 |

## 已定决策（不可推翻，改了要先写 ADR）

1. **不做回写分发。** 不向 Codex / Claude / Cursor 的原生记忆文件写入任何内容。只读采集 + 单向生成只读快照。理由见 `docs/01-decisions.md` ADR-002。
2. **三级信任替代逐条审核。** `auto`（低风险自动入库，靠 Git 可回滚）/ `review`（进 inbox 等人批）/ `blocked`（敏感或冲突，阻断）。见 ADR-003。
3. **先不做 Web UI。** 阶段 1-3 只有 CLI 与 MCP。见 ADR-004。
4. **代码与数据物理分离。** 本仓库不含 vault；vault 路径由配置提供，默认 `D:/my-memory`。
5. **单一 Core API。** 所有写操作必须经过 `memoryhub.storage`，Adapter 与 CLI 一律不得直接写文件。
6. **Markdown 是唯一真相源。** 任何索引、SQLite、缓存都必须可从 Markdown 重建；不得出现只能从索引读到的记忆。
7. **阶段 1 零第三方运行时依赖。** 只用标准库。原因见 ADR-005。

## 目录职责

```
src/memoryhub/
  config.py              # 配置读写（TOML）、vault 路径解析
  models.py              # MemoryEntry 领域模型与校验
  frontmatter.py         # frontmatter 最小解析/序列化（阶段 1 实现，阶段 2 换真 YAML 库）
  storage/markdown_store.py  # 唯一写入入口：原子写、文件锁、扫描、检索
  cli.py                 # CLI 入口
tests/                   # unittest，必须能在无网络、无第三方包的机器上跑通
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

## 验收方式

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m memoryhub doctor
```

两项都通过才算完成一次改动。新增功能必须补测试；只加功能不加测试的提交视为未完成。
