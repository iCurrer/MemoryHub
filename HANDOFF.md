# HANDOFF · 项目交接

> 最后更新：2026-11-24 14:52 ｜ 当前阶段：**阶段 1 + 1.5 完成，P2-01 完成，阶段 2 其余未开始**

任何接手的 AI Agent：**先读 `AGENTS.md`（工程契约），再读本文件（现状），最后读 `docs/02-roadmap.md` 领工单。** 不要跳过前三者直接改代码。

---

## 1. 一句话现状

阶段 1 核心数据层已跑通：记忆可以创建、落盘、审批、检索、归档，有原子写入和文件锁保护，**45 个单元测试全绿**。阶段 1.5 的加固把三处会静默丢数据/失联的路径堵掉了（见 ADR-009）；P2-01 把 frontmatter 换成真 YAML，**手改文件不再读不出来**（ADR-005 部分取代）。上游来源的路径与格式已实测核实（[`docs/04-upstream-formats.md`](docs/04-upstream-formats.md)）。**采集适配器、AI 整理、MCP、备份命令都还没有。**

## 2. 仓库信息

| 项 | 值 |
|---|---|
| 本地路径 | `D:/project/MemoryHub` |
| 远端 | `origin → https://github.com/iCurrer/MemoryHub.git`（**公开仓**） |
| 分支 | `main` |
| 提交 | 骨架 2 个 + 加固 1 个 + 上游基线 1 个 + P2-01 1 个 |
| 数据目录 | `D:/my-memory`（**尚未创建**，需 `memoryhub init` 生成） |
| 运行时依赖 | **`pyyaml>=6.0`**（第一个，也是目前唯一一个） |

## 3. 已实现（可依赖）

| 文件 | 职责 | 稳定性 |
|---|---|---|
| `src/memoryhub/models.py` | `MemoryEntry` 领域模型、三级信任推导、校验、`is_expired`、Markdown 序列化 | 稳定，字段已冻结（`project` 分类的信任归属待拍板，见 ADR-010） |
| `src/memoryhub/frontmatter.py` | PyYAML 解析/序列化：嵌套、多行、块列表、BOM、重复键拒绝 | **稳定**（P2-01 完成） |
| `src/memoryhub/storage/markdown_store.py` | 唯一写入入口：原子写、文件锁、`create()`/`save()`、扫描、检索、状态流转、取代链 | 稳定 |
| `src/memoryhub/config.py` | TOML 配置、vault 路径三级解析 | 稳定（注意：`save()` 会整份重写，加配置项前先改成合并写入） |
| `src/memoryhub/cli.py` | 9 个命令 | 稳定，命令会继续增加 |
| `tests/test_store.py` | 30 个用例 | 会随功能增长 |
| `tests/test_cli.py` | 15 个端到端用例 | 覆盖 `--vault` 三写法、`--approve` 分级、错误路径 |

可用命令：`init` `doctor` `new` `list` `show` `search` `approve` `reject` `stats`

本轮新增的行为（都带测试）：

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

python -m unittest discover -s tests -v          # 期望 45 passed
python -m memoryhub --vault /tmp/v --version     # 期望版本号
python -m memoryhub init --vault /tmp/v          # 期望创建 7 个目录
python -m memoryhub doctor --vault /tmp/v        # 期望 exit 0（git 项 WARN 也算通过）
```

注意：`doctor` 在**未 init 的仓上应返回 1**（vault 与 layout 缺失是 FAIL），这是正确行为。
`init --no-git` 之后应为 exit 0 —— 这是本轮修掉的门禁矛盾。

## 5. 未实现（不要以为已有）

- 采集：无任何 adapter，`sources/` 目录是空的
- 监控：无 Watchdog，改动来源文件不会被发现
- 索引：无 SQLite，`search` 是遍历目录（400 条约 114ms，可接受）
- AI：无 Provider，无去重/冲突检测
- MCP：无 server
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

以及 ADR-011（三条一起拍）：`source_kind` 扩展、`precedence`/`layer` 字段、"Markdown 只是输出侧的真相源"。**P2-03 开工前必须定。**

## 8. 下一步

**P2-01 已完成。** 现在有两条路，按依赖关系排序：

1. **P2-02（SQLite/FTS5 索引）** —— **不依赖任何待决事项，可以立刻开工**。`search` 目前是目录扫描：实测 400 条 warm 检索 114ms，可用但会随条目线性增长；中文也是子串匹配而非分词。
2. **P2-03（Adapter 接口）** —— **卡 ADR-011**（`source_kind` 扩展、`precedence`/`layer` 字段、"Markdown 只是输出真相源"）。不拍板就开，等于用错的形状设计接口，之后要改两次。

我的建议顺序仍是"先闭环后索引"（见 roadmap 末尾"建议调整"），但那需要先拍 ADR-011。**在拍板之前，P2-02 是唯一能推进的工单。**

**关于首个适配器选谁**（建议，未生效）：按 [`docs/04-upstream-formats.md`](docs/04-upstream-formats.md) 的证据，Trae CN 与 WorkBuddy 是**纯 Markdown / JSONL，零解压、零 SQLite**，比 Codex（zstd-jsonl + sqlite）简单得多。若目标是"两周内跑通一次真实闭环"，首个适配器选它们更划算；Codex 的指令层仍应做，因为它是跨工具的公共格式。

## 9. 给接手者的提醒

- 本项目的核心资产是**数据格式与治理规则**，不是代码量。宁可不写功能，不要改字段语义。
- 每次改动跑完两条验收命令再提交，不要攒大批量改动。
- 策划书（`docs/reference/`）是历史输入，不是需求来源。有疑问查 `docs/03-plan-review.md` 的对照表。
- CLI 测试用 `MEMORYHUB_HOME` 隔离配置目录，别让它读到真实 `~/.memoryhub`。
- 测试需要 `pyyaml`。`AGENTS.md` 里"无第三方包也能跑通"这句话对阶段 2 已不成立，见 ADR-005 与已知问题 #8。
