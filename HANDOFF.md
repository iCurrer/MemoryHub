# HANDOFF · 项目交接

> 最后更新：2026-11-24 14:05 ｜ 当前阶段：**阶段 1 完成，阶段 2 未开始**

任何接手的 AI Agent：**先读 `AGENTS.md`（工程契约），再读本文件（现状），最后读 `docs/02-roadmap.md` 领工单。** 不要跳过前三者直接改代码。

---

## 1. 一句话现状

阶段 1 核心数据层已跑通：记忆可以创建、落盘、审批、检索、归档，有原子写入和文件锁保护，15 个单元测试全绿。**采集适配器、AI 整理、MCP、备份命令都还没有。**

## 2. 仓库信息

| 项 | 值 |
|---|---|
| 本地路径 | `D:/project/MemoryHub` |
| 远端 | `origin → https://github.com/iCurrer/MemoryHub.git`（**公开仓，空仓待推**） |
| 分支 | `main` |
| 提交 | 阶段 1 骨架 1 个提交 |
| 数据目录 | `D:/my-memory`（**尚未创建**，需 `memoryhub init` 生成） |

## 3. 已实现（可依赖）

| 文件 | 职责 | 稳定性 |
|---|---|---|
| `src/memoryhub/models.py` | `MemoryEntry` 领域模型、三级信任推导、校验、Markdown 序列化 | 稳定，字段已冻结 |
| `src/memoryhub/frontmatter.py` | 受限 YAML 子集解析/序列化 | **临时实现**，P2-01 替换 |
| `src/memoryhub/storage/markdown_store.py` | 唯一写入入口：原子写、文件锁、扫描、检索、状态流转、取代链 | 稳定 |
| `src/memoryhub/config.py` | TOML 配置、vault 路径三级解析 | 稳定 |
| `src/memoryhub/cli.py` | 9 个命令 | 稳定，命令会继续增加 |
| `tests/test_store.py` | 15 个用例 | 会随功能增长 |

可用命令：`init` `doctor` `new` `list` `show` `search` `approve` `reject` `stats`

## 4. 怎么跑（复制粘贴即可验证）

```bash
cd /d/project/MemoryHub
export PYTHONPATH=src

python -m unittest discover -s tests -v          # 期望 15 passed
python -m memoryhub --vault /tmp/v --version     # 期望版本号
python -m memoryhub init --vault /tmp/v --no-git # 期望创建 7 个目录
python -m memoryhub doctor                       # 期望无 FAIL（git 项会 FAIL，正常）
```

## 5. 未实现（不要以为已有）

- 采集：无任何 adapter，`sources/` 目录是空的
- 监控：无 Watchdog，改动来源文件不会被发现
- 索引：无 SQLite，`search` 是遍历目录
- AI：无 Provider，无去重/冲突检测
- MCP：无 server
- 备份：无 `backup` 命令，Git 需手工执行

## 6. 已知问题（按优先级）

| # | 问题 | 影响 | 归属 |
|---|---|---|---|
| 1 | frontmatter 只支持扁平 key + 内联列表 | 生成的 `.md` 不能被 Obsidian 正确解析，"人类可读"卖点打折 | P2-01 |
| 2 | `doctor` 与日志输出绝对路径（含用户名） | 公开仓 issue 贴日志会泄露目录结构与用户名 | **待拍板**，加 `redact_path()` |
| 3 | `cli.py` 中 `doctor` 对条目做了两次遍历 | 大库时变慢 | 随 P2-02 一并解决 |
| 4 | 锁是自制 O_EXCL + 120 秒陈旧清理 | 极端并发下有竞态 | 阶段 5 评估 |
| 5 | 仅在 Python 3.13 验证，声明 `>=3.11` | 3.11 兼容性未测 | 加 CI 矩阵 |
| 6 | 无 `LICENSE` 文件 | 公开仓默认"保留所有权利"，与开源定位矛盾 | **待拍板**，倾向 Apache-2.0 |

## 7. 决策边界（改之前必须看）

六条不可推翻的决策在 `AGENTS.md`，完整推理在 `docs/01-decisions.md`。最容易被误做的三条：

1. **不要实现"把中央记忆写回各 AI 工具"** —— 这是策划书原方案，已否决（ADR-002）。看到 `exports/` 就想"顺手写回去"是本项目第一大坑。
2. **不要让 AI 产出直接进 `vault/`** —— 必须 `status=pending`（ADR-003）。
3. **不要把 `vault/` 放进本仓库** —— 哪怕只是"临时测试"（ADR-007）。

## 8. 下一步

执行 `docs/02-roadmap.md` 的 **P2-01**：把 `frontmatter.py` 迁到 PyYAML。

这是阶段 2 的第一块砖，理由：它解掉已知问题 #1，且后面所有 adapter 解析真实 `AGENTS.md` 时都依赖正确的 YAML。在它完成前，不要动 adapter 代码——否则会基于错误的数据格式写两遍。

## 9. 给接手者的提醒

- 本项目的核心资产是**数据格式与治理规则**，不是代码量。宁可不写功能，不要改字段语义。
- 每次改动跑完两条验收命令再提交，不要攒大批量改动。
- 策划书（`docs/reference/`）是历史输入，不是需求来源。有疑问查 `docs/03-plan-review.md` 的对照表。
