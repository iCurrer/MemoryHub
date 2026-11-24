# HANDOFF · 项目交接

> 最后更新：2026-11-24 14:56 ｜ 当前阶段：**阶段 1 + 1.5 + P2-01 + P2-03/03a 完成，阶段 2 其余未开始**

任何接手的 AI Agent：**先读 `AGENTS.md`（工程契约），再读本文件（现状），最后读 `docs/02-roadmap.md` 领工单。** 不要跳过前三者直接改代码。

---

## 1. 一句话现状

**第一个真实来源已经打通**：`memoryhub scan` 能把 WorkBuddy 的用户记忆读成 `inbox/pending` 条目，重复扫描不产生重复条目，审核后进 `vault/` 可被 `search` 找到。**62 个单元测试全绿**。frontmatter 已换真 YAML（P2-01），手改文件不再读不出来。阶段 1.5 堵掉了三处静默丢数据的路径（ADR-009）。上游格式基线已实测核实（[`docs/04-upstream-formats.md`](docs/04-upstream-formats.md)）。**Codex / Claude 适配器、监控、索引、AI 整理、MCP、备份都还没有。**

## 2. 仓库信息

| 项 | 值 |
|---|---|
| 本地路径 | `D:/project/MemoryHub` |
| 远端 | `origin → https://github.com/iCurrer/MemoryHub.git`（**公开仓**） |
| 分支 | `main` |
| 提交 | 骨架 2 个 + 加固 1 个 + 上游基线 1 个 + P2-01 1 个 + 采集 1 个 |
| 数据目录 | `D:/my-memory`（**尚未创建**，需 `memoryhub init` 生成） |
| 运行时依赖 | **`pyyaml>=6.0`**（第一个，也是目前唯一一个） |

## 3. 已实现（可依赖）

| 文件 | 职责 | 稳定性 |
|---|---|---|
| `src/memoryhub/models.py` | `MemoryEntry` 领域模型、三级信任推导、校验、`is_expired`、`precedence`/`layer`、Markdown 序列化 | 稳定，字段已冻结（`project` 分类的信任归属待拍板，见 ADR-010） |
| `src/memoryhub/frontmatter.py` | PyYAML 解析/序列化：嵌套、多行、块列表、BOM、重复键拒绝 | **稳定**（P2-01 完成） |
| `src/memoryhub/adapters/base.py` | `Adapter` 协议、`SourcePath`（带 `precedence`/`layer`）、markdown 切分 helper | 稳定（P2-03） |
| `src/memoryhub/adapters/workbuddy.py` | 首个真实适配器：`~/.workbuddy/memory/*_memory.md` | 稳定（P2-03a），格式依据见 docs/04 |
| `src/memoryhub/storage/markdown_store.py` | 唯一写入入口：原子写、文件锁、`create()`/`save()`、扫描、检索、状态流转、取代链 | 稳定 |
| `src/memoryhub/config.py` | TOML 配置、vault 路径三级解析 | 稳定（注意：`save()` 会整份重写，加配置项前先改成合并写入） |
| `src/memoryhub/cli.py` | 10 个命令 | 稳定，命令会继续增加 |
| `tests/test_store.py` | 30 个用例 | 会随功能增长 |
| `tests/test_cli.py` | 19 个端到端用例 | 覆盖 `--vault` 三写法、`--approve` 分级、`scan` 幂等、错误路径 |
| `tests/test_adapters.py` | 13 个用例 | 协议三条判据 + WorkBuddy 解析 |

可用命令：`init` `doctor` `new` `scan` `list` `show` `search` `approve` `reject` `stats`

本轮新增的行为（都带测试）：

- **`scan`**：从已注册的 adapter 采集到 `inbox/pending`，按 `(adapter, source_path, body)` 指纹去重，可 `--dry-run`；adapter 恒为只读
- `new` 走 `store.create()`：id 被占用时**报错而不是覆盖**
- `new --approve` 遇到 `trust=review` 会拒绝，需 `--force` 才越过；`blocked` 无论如何不可越过
- `list` / `search` 默认隐藏 `valid_until` 已过期的条目，`--include-expired` 打开；
  显式 `--status` 视为历史查询，不再二次过滤
- `doctor` 分 `FAIL`（阻断）与 `WARN`（不阻断：config 未生成、vault 未 git init、OneDrive）
- 读不了的 `.md` 不再静默跳过：`list`/`search` 汇总告警，`stats` 计数，`doctor` 判 FAIL
- `--vault` 在子命令前后都生效（此前 `memoryhub --vault X init` 会静默写到默认盘）

## 4. 怎么跑（复制粘贴即可验证）

```bash
cd /d/project/MemoryHub
pip install pyyaml          # 唯一的运行时依赖
export PYTHONPATH=src

python -m unittest discover -s tests -v          # 期望 62 passed
python -m memoryhub --vault /tmp/v --version     # 期望版本号
python -m memoryhub init --vault /tmp/v          # 期望创建 7 个目录
python -m memoryhub doctor --vault /tmp/v        # 期望 exit 0（git 项 WARN 也算通过）
python -m memoryhub scan --vault /tmp/v --dry-run # 期望列出发现的来源与将导入条数
```

注意：`doctor` 在**未 init 的仓上应返回 1**（vault 与 layout 缺失是 FAIL），这是正确行为。
`init --no-git` 之后应为 exit 0 —— 这是本轮修掉的门禁矛盾。

## 5. 未实现（不要以为已有）

- 采集：**只有 WorkBuddy 一个**（`auto_memory` 类）。Codex（指令链 + sqlite + zstd）、Claude Code（CLAUDE.md + `memory/`）、Cursor（`.mdc`）、Trae（`user_profile` / `project_memory` / `session_memory`）都还没有
- 监控：无 Watchdog，改动来源文件不会被发现，`scan` 要手动跑
- 索引：无 SQLite，`search` 是遍历目录（400 条约 114ms，可接受）
- AI：无 Provider，无去重/冲突检测；`scan` 只做规则级切分，**语义去重与冲突一律没有**
- MCP：无 server —— **这意味着别的 AI 现在还读不到中央库**（见第 8 节）
- 备份：无 `backup` 命令，Git 需手工执行

## 6. 已知问题（按优先级）

| # | 问题 | 影响 | 归属 |
|---|---|---|---|
| 1 | `doctor` 与日志输出绝对路径（含用户名） | 公开仓 issue 贴日志会泄露目录结构与用户名 | **待拍板**，加 `redact_path()` |
| 2 | 锁是自制 O_EXCL + 120 秒陈旧清理；`__exit__` 无条件删锁文件 | 极端并发下有竞态；多进程（MCP + watcher）是阶段 5 的常态 | 阶段 5 评估 |
| 3 | 仅在 Python 3.12/3.13 验证，声明 `>=3.11` | 3.11 兼容性未测（`pyyaml` 在 3.11 上的轮子也未验证） | 加 CI 矩阵 |
| 4 | 无 `LICENSE` 文件 | 公开仓默认"保留所有权利"，与开源定位矛盾 | **待拍板**，倾向 Apache-2.0 |
| 5 | `config.save()` 整份重写配置 | 阶段 3 加 `ai.*` 后，`init` 会把它们抹掉 | 阶段 3 前改成合并 |
| 6 | `supersedes` 不做引用存在性校验，可悬空 | 取代链是核心资产，却没有完整性检查 | 阶段 3 冲突检测时补 |
| 7 | `status` 字段与文件位置是两个真相源 | 手改 `status` 不会移动文件，`list` 会出现躺在 inbox 里的 approved | 阶段 4 差异预览时统一 |
| 8 | `AGENTS.md`「测试必须能在无第三方包的机器上跑通」已不成立 | 新增了 `pyyaml` 依赖，新人按契约描述会以为不用装 | **待所有者改措辞**，见 ADR-005 |

（已清：frontmatter 读不出块列表 —— P2-01 解决，见 ADR-005。）

## 7. 决策边界（改之前必须看）

不可推翻的决策在 `AGENTS.md`（7 条）与 `docs/01-decisions.md`（ADR-001 至 ADR-011，其中 ADR-010 / ADR-011 待拍板）。最容易被误做的三条：

1. **不要实现"把中央记忆写回各 AI 工具"** —— 这是策划书原方案，已否决（ADR-002）。看到 `exports/` 就想"顺手写回去"是本项目第一大坑。
2. **不要让 AI 产出直接进 `vault/`** —— 必须 `status=pending`，`review` 级别要人批（ADR-003，本轮已把 `--approve` 的绕过口封上）。
3. **不要把 `vault/` 放进本仓库** —— 哪怕只是"临时测试"（ADR-007）。

还有一条**未决**：`project` 分类在 ADR-003 的信任表里没有归属，当前落 `auto`。这是代码替人做的决定，见 ADR-010，需要所有者二选一。

ADR-011 已采纳（`source_kind` 扩展、`precedence`/`layer` 字段、"Markdown 只是输出侧真相源"），实现见 `adapters/base.py`。

## 8. 下一步

**闭环已通，但还缺"出口"。** 现在记忆能进来、能审、能查，但**只有 CLI 能查** —— 别的 AI 工具一个都读不到。按价值排序：

1. **P5-01 最小 MCP（只读三件套：`memory_search` / `memory_get` / `memory_list`）** —— 建议提前。这是"让别的 AI 用上你的记忆"的唯一正确通路（回写分发已由 ADR-002 否决）。做完之后 Cursor / Claude Desktop 配一个 MCP server 就能直接查中央库。
2. **P2-04 / P2-05 第二个适配器** —— Codex 是跨工具公共格式，Claude 的记忆层已有 `type`/`modified` 可直接映射（见 docs/04 §3.3）。
3. **P2-02 索引** —— 数据量还很小，随时可做但不紧急。

**关于"分发"这个问题的现状**（避免下一任误解）：`exports/` 目录存在但**没有任何代码写它**，这是 ADR-002 的有意结果，不是遗漏。若要"推"的效果，唯一可接受形式是只写进 MemoryHub 自己的命名空间再由用户 `@import`，那需要新 ADR。

**待所有者执行的 AGENTS 修改**（我不能代改契约文件）：
1. 「已定决策」第 6 条需加限定："Markdown 是唯一真相源**仅指输出侧（vault）**，输入侧允许 md / jsonl / zstd-jsonl / sqlite"（ADR-011 第 3 条）。
2. 「目录职责」应补 `adapters/`。
3. `tests/` 那条"必须能在无第三方包的机器上跑通"对阶段 2 已不成立（现有 `pyyaml`）。
4. 「当前阶段」段落仍写着"阶段 2 及以后尚未实现"，实际已完成 P2-01 / P2-03 / P2-03a。

## 9. 给接手者的提醒

- 本项目的核心资产是**数据格式与治理规则**，不是代码量。宁可不写功能，不要改字段语义。
- 每次改动跑完两条验收命令再提交，不要攒大批量改动。
- 策划书（`docs/reference/`）是历史输入，不是需求来源。有疑问查 `docs/03-plan-review.md` 的对照表。
- CLI 测试用 `MEMORYHUB_HOME` 隔离配置目录，别让它读到真实 `~/.memoryhub`。
- 测试需要 `pyyaml`。`AGENTS.md` 里"无第三方包也能跑通"这句话对阶段 2 已不成立，见 ADR-005 与已知问题 #8。
