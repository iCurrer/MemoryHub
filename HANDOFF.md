# HANDOFF · 项目交接

> 最后更新：2026-11-25 ｜ 当前阶段：**阶段 1 / 1.5 / 2（除 P2-03b）/ 3（P3-01/02 完成、P3-03 部分）/ 4 / 5 完成**

任何接手的 AI Agent：**先读 `AGENTS.md`（工程契约），再读本文件（现状），最后读 `docs/02-roadmap.md` 领工单。** 不要跳过前三者直接改代码。

---

## 1. 一句话现状

**三个来源（WorkBuddy / Claude Code / Codex）→ 整理 → 审核 → vault → CLI / MCP 检索，全链路已通。** `import` 采集进 `inbox/pending`（加 `--ai` 则把原文交给用户自己的模型拆分与分类），`diff`/`approve` 审核入库，`search` 走 SQLite FTS5 索引，`mcp` 提供 5 个工具并按 `--permission` 分级，`backup` 带敏感闸门。**213 个单元测试**（本机缺 `mcp` SDK 时 `test_mcp` 无法导入，属可选依赖缺失，不是回归）。**未做：语义去重与冲突检测（P3-04/05）、P3-00 fallback 显式化、回写分发、阶段 6 打包与 Web UI、P2-03b、Codex 记忆层。**

**AI 整理怎么工作（ADR-016）**：`Adapter.discover()` 仍是算法（找文件是确定的）；`read()` 在 `--ai` 模式改走模型（拆分 + 分类是语义的）；模型返回的每一条必须带从原文逐字复制的 `quote`，归一化后定位不到就标记待核实。**这条 quote 校验是唯一能自动化的防幻觉手段，不要删。**

**当前最大断点（2026-11-25）**：`D:/my-memory` 里 53 条全是 `pending`，`approved` 为 0。MCP 的可见性规则（ADR-012）只返回 `approved`，所以**别的 AI 现在查不到任何东西**。`doctor` 的 `mcp visibility` 会明确报这一条。下一步不是加功能，是让人把这 53 条审完——或者先做 P4-05 交互式审核。

## 2. 仓库信息

| 项 | 值 |
|---|---|
| 本地路径 | `D:/project/MemoryHub` |
| 远端 | `origin → https://github.com/iCurrer/MemoryHub.git`（**公开仓**） |
| 分支 | `main` |
| 提交 | 34（2026-11-25 曾对全部历史做一次提交信息改写；随后删除并重建远端仓，旧对象已不存在） |
| 数据目录 | `D:/my-memory`（已创建，59 条：53 pending + 6 archived，**自身独立 git 仓、无远端**） |
| 运行时依赖 | **`pyyaml>=6.0`、`mcp>=2.0,<3`**（后者连带 pydantic/starlette/uvicorn 等，见 ADR-012） |

### 2.1 三层边界（ADR-013，别搞混）

| 层 | 路径 | 远端 | 守住它的东西 |
|---|---|---|---|
| 代码仓 | `D:/project/MemoryHub` | `origin` → GitHub 公开仓 | `tools/hooks/pre-commit`（暂存内容）+ `commit-msg`（提交信息） |
| 记忆仓工作副本 | `D:/my-memory` | 无 | `boundary.violations()`：位于任何 git 工作树内即拒绝 `init` |
| 记忆仓远端 | 用户自选（建议私有 + 加密） | 由用户决定 | `init` 写入的 `pre-push` 钩子：URL 含 memoryhub 即拒推 |

**云端只有代码仓。** 用户记忆永不进它，也不进它的历史。clone 后先跑 `sh tools/setup_dev.sh`，否则钩子不生效（未启用时 `python tools/lint_staged.py --all` 仍可手动扫）。


## 3. 已实现（可依赖）

| 文件 | 职责 | 稳定性 |
|---|---|---|
| `src/memoryhub/models.py` | `MemoryEntry` 领域模型、三级信任推导、校验、`is_expired`、`precedence`/`layer`、Markdown 序列化 | 稳定，字段已冻结（`project` 分类的信任归属待拍板，见 ADR-010） |
| `src/memoryhub/frontmatter.py` | PyYAML 解析/序列化：嵌套、多行、块列表、BOM、重复键拒绝 | **稳定**（P2-01 完成） |
| `src/memoryhub/adapters/base.py` | `Adapter` 协议、`SourcePath`（带 `precedence`/`layer`）、markdown 切分 helper | 稳定（P2-03） |
| `src/memoryhub/adapters/workbuddy.py` | 两种形态：`memory/<uuid>_memory.md` 与 `user-<uuid>-personal/`（IDENTITY/SOUL/USER/MEMORY） | 稳定（P2-03a），格式依据见 docs/04 §5 |
| `src/memoryhub/adapters/claude.py` | `CLAUDE.md` 链 + auto memory 适配器 | 指令层**已真机验证**；auto memory 层仍待真数据（P2-05） |
| `src/memoryhub/adapters/codex.py` | Codex 指令链（override 遮蔽、32 KiB 上限）**已用真 codex exec 验证** | 稳定（P2-04） |
| `src/memoryhub/backup/git.py` | vault 的 git 操作 + 推送前敏感闸门 | 稳定（P5-04） |
| `src/memoryhub/backup/history.py` | 从 vault 的 git log 读变更摘要（`log`） | 稳定（P4-03） |
| `src/memoryhub/storage/index.py` | SQLite FTS5 索引：trigram、mtime 新鲜度、可重建 | 稳定（P2-02） |
| `src/memoryhub/watcher/` | 轮询 + 防抖 + 哈希去重（`MtimeTracker`→`Debouncer`→`HashTracker`） | 稳定（P2-06） |
| `src/memoryhub/mcp/server.py` | MCP 5 工具 + 可见性规则 + read/propose/admin 权限分级（ADR-012） | 稳定（P5-01、P5-02） |
| `src/memoryhub/storage/markdown_store.py` | 唯一写入入口：原子写、文件锁、`create()`/`save()`、扫描、检索、状态流转、取代链 | 稳定 |
| `src/memoryhub/config.py` | TOML 配置、vault 路径三级解析、`save()` 支持嵌套表与读-合并-写 | 稳定（2026-11-25 已改为合并写入） |
| `src/memoryhub/boundary.py` | 两仓隔离：`violations()`、远端检查、`pre-push` 钩子安装 | 稳定（ADR-013） |
| `src/memoryhub/classify.py` | 加权规则分类（决定性权重 3），实测 52.8%，**仅作离线兜底**（ADR-015） | 降级：AI 模式下不参与 |
| `src/memoryhub/ai/provider.py` | OpenAI 兼容 `chat/completions`，标准库 `urllib`，key 只读环境变量 | 稳定（ADR-016） |
| `src/memoryhub/ai/schema.py` | 手写结构化校验：逐条丢弃、`quote` 必填、`body` 上限 1200 字 | 稳定，**不要换成 Pydantic**（ADR-005） |
| `src/memoryhub/ai/organize.py` | 提示词 + 分批（24k 字）+ `quote` 定位校验 + 转 `pending` 条目 | 稳定；提示词是规格，改前先读文件头注释 |
| `tools/audit_classify.py` | 规则分类对账：三层准确率 + 混淆矩阵，`--min-accuracy` 可作 CI 门禁 | 稳定，标注文件在 `docs/audits/` |
| `tools/lint_staged.py` | 暂存内容 + 提交信息的路径/PII 扫描；`--all` 全量、`--message-only` | 稳定（ADR-013） |
| `tools/hooks/` + `tools/setup_dev.sh` | `core.hooksPath` 钩子，clone 后跑一次 | 稳定；新增规则改 `RULES` 与 `pii-allowlist.txt` |
| `src/memoryhub/cli.py` | **五条主线**：`init` `import` `approve` `mcp` `doctor`；其余命令保留但移出帮助表 | 稳定（ADR-016） |
| `tests/test_store.py` | 37 个用例 | 会随功能增长 |
| `tests/test_cli.py` | 38 个端到端用例（含审核流、帮助页收窄、`--ai` 错误路径） | 覆盖 `--vault` 三写法、`--approve` 分级、`scan` 幂等、错误路径 |
| `tests/test_adapters.py` | 33 个用例 | 协议三条判据 + WorkBuddy（含人格目录）+ Claude + Codex 适配器 |
| `tests/test_mcp.py` | 19 个用例 | 可见性规则 + 真实 stdio 握手（含 stdout 纯净性断言） |
| `tests/test_watcher.py` | 11 个用例 | 防抖/哈希/排除 + 注入时钟 |
| `tests/test_index.py` | 9 个用例 | 中文、删库重建一致、外部编辑、兜底 |
| `tests/test_history.py` | 8 个用例 | 真实 git 仓：新增/修改/删除、`--since` 翻译、非仓与空仓 |
| `tests/test_backup.py` | 8 个用例 | 本地提交 / 远端推送敏感闸门 / .gitignore |
| `tests/test_safety.py` | 6 个用例 | 凭证与 PII 分级 |
| `tests/test_ai.py` | 27 个用例 | 假传输桩：提示词契约、JSON 提取、逐条校验、quote 定位、配置读取 |
| `tests/test_boundary.py` | 15 个用例 | 两仓隔离：vault 在 git 树内即拒、pre-push 钩子、远端 URL 判定 |
| `tests/test_classify.py` | 15 个用例 | 加权规则、决定性权重、平票优先级 |
| `tests/test_migrate.py` | 5 个用例 | 长条目切分三级回退、空标题剔除 |

可用命令（帮助页只列前五条，其余仍可调用）：

`init` `import`（别名 `scan`，加 `--ai` 走模型）`approve` `mcp` `doctor` ｜ 审核辅助：`list` `show` `search` `diff` `reject` ｜ 高级：`new` `watch` `index` `audit` `log` `sensitivity` `recategorize` `backup` `stats`

本轮新增的行为（都带测试）：

- **`scan`**：从已注册的 adapter 采集到 `inbox/pending`，按 `(adapter, source_path, body)` 指纹去重，可 `--dry-run`；adapter 恒为只读
- **`mcp`**：stdio MCP server，5 个工具（`memory_status` 仅 admin）；`search`/`list` 只暴露 `approved` 且未过期，`get` 可查任意状态并标注，`propose` 只能产生 `pending`（命中密钥形状即 `blocked`）
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
pip install pyyaml mcp      # 运行时依赖
export PYTHONPATH=src

python -m unittest discover -s tests -v          # 期望 Ran 213；缺 mcp 时 1 error（test_mcp 导入），212 通过
python -m memoryhub --vault /tmp/v --version     # 期望版本号
python -m memoryhub init --vault /tmp/v          # 期望创建 8 个目录（含 `database/`）
python -m memoryhub doctor --vault /tmp/v        # 期望 exit 0（git 项 WARN 也算通过）
python -m memoryhub scan --vault /tmp/v --dry-run # 期望列出发现的来源与将导入条数
python -m memoryhub mcp --vault /tmp/v            # 阻塞，等 MCP 客户端连上来
```

注意：`doctor` 在**未 init 的仓上应返回 1**（vault 与 layout 缺失是 FAIL），这是正确行为。
`init --no-git` 之后应为 exit 0 —— 这是本轮修掉的门禁矛盾。

## 5. 未实现（不要以为已有）

- 采集：**三个适配器**（WorkBuddy / Claude / Codex）。WorkBuddy 覆盖两种形态（`memory/*_memory.md`、`user-*-personal/`），**工作区级 `<workspace>/.workbuddy/memory/` 未做（P2-03b）**；Claude Code 覆盖 `CLAUDE.md` 链（**指令层已真机验证**）+ auto memory（**目录存在但为空，未验证**）；Codex 只做了指令层，**记忆层（`memories_1.sqlite` + `sessions/*.jsonl`）未做**。Cursor / Trae 都没有
- 监控：**有轮询 watcher（`watch`）**，2 秒间隔 + 防抖 + 哈希；没有 `watchdog`（有意，见 roadmap P2-06）
- 索引：**有**（`database/index.db`，FTS5 trigram + `LIKE` 兜底），`search` 自动建/重建；坏掉会退回全表扫描
- AI：**有 Provider（P3-01）**，`import --ai` 把原文交给用户自己的模型做拆分与分类；**没有**摘要字段（P3-03 只完成一半）、**没有**语义去重与冲突检测（P3-04/05）；回写分发未实现（ADR-002 未推翻，方向见 §7 第 1 条的 2026-11-25 复审记录）
- MCP：`--permission read|propose|admin`（默认 propose）；read=3 工具、propose=+propose、admin=+status。**任何级别都没有 `memory_write`**、**没有** HTTP 传输、**没有** 认证
- 备份：**有** `backup`（本地提交 + 可选远端 + 敏感闸门 + 自动 .gitignore）

### 接到 AI 客户端（已验证协议，未在该客户端实测）

服务端已用**真 subprocess 跑过完整握手**（`initialize` → `tools/list` → `tools/call`，并断言 stdout 只有 JSON-RPC）。但**没有在 Claude Desktop / Cursor 里实机点过**，接线时若连不上，先看客户端日志。

```json
{
  "mcpServers": {
    "memoryhub": {
      "command": "python",
      "args": ["-m", "memoryhub", "mcp", "--vault", "D:/my-memory"],
      "env": { "PYTHONPATH": "D:/project/MemoryHub/src" }
    }
  }
}
```

Claude Desktop 放在 `%APPDATA%\Claude\claude_desktop_config.json`；Cursor 放在项目内 `.cursor/mcp.json`。

## 6. 已知问题（按优先级）

| # | 问题 | 影响 | 归属 |
|---|---|---|---|
| 1 | `doctor` 与日志输出绝对路径（含用户名） | 公开仓 issue 贴日志会泄露目录结构与用户名 | **待拍板**，加 `redact_path()` |
| 2 | 敏感检测覆盖凭证 + 强 PII（身份证/手机/住址）；**弱 PII 只标 personal** | 姓名、亲属关系等语义型隐私仍认不出；`personal` 级别不阻断 | 已够用；如需更强需语义判断（阶段 3） |
| 3 | 锁是自制 O_EXCL + 120 秒陈旧清理；`__exit__` 无条件删锁文件 | 极端并发下有竞态；多进程（MCP + watcher）是阶段 5 的常态 | 阶段 5 评估 |
| 3 | 仅在 Python 3.12/3.13 验证，声明 `>=3.11` | 3.11 兼容性未测（`pyyaml` 在 3.11 上的轮子也未验证） | 加 CI 矩阵 |
| 4 | 无 `LICENSE` 文件 | 公开仓默认"保留所有权利"，与开源定位矛盾 | **待拍板**，倾向 Apache-2.0 |
| 5 | ~~`config.save()` 整份重写配置~~ | 阶段 3 加 `ai.*` 后 `init` 会抹掉它们 | **已解决**（2026-11-25：`save()` 支持嵌套表，`init` 改为读-合并-写，`[ai]` 保留有测试） |
| 6 | `supersedes` 不做引用存在性校验，可悬空 | 取代链是核心资产，却没有完整性检查 | 阶段 3 冲突检测时补 |
| 7 | `status` 字段与文件位置是两个真相源 | 手改 `status` 不会移动文件。保存过期快照曾把已批准条目搬回 inbox（P4-03 写测试时实测到）。**已加乐观并发校验**：`_base_updated_at` 与磁盘不一致时抛 `StaleEntryError`。剩余局限：时间戳是秒级，同一秒内的并发写入检测不到 | 已修复主路径；如需同秒保护，把 `now_iso` 改成微秒 |
| 8 | `AGENTS.md`「测试必须能在无第三方包的机器上跑通」已不成立 | 新增了 `pyyaml` 依赖，新人按契约描述会以为不用装 | **已解决**（2026-11-25，AGENTS.md 已改为"需 pyyaml + mcp，无需网络"） |

（已清：frontmatter 读不出块列表 —— P2-01 解决，见 ADR-005。）

## 7. 决策边界（改之前必须看）

不可推翻的决策在 `AGENTS.md`（7 条）与 `docs/01-decisions.md`（ADR-001 至 ADR-016，其中 ADR-010 / ADR-011 / ADR-015 的三项变更待拍板）。最容易被误做的三条：

1. **不要实现"把中央记忆写回各 AI 工具"** —— 这是策划书原方案，已否决（ADR-002）。看到 `exports/` 就想"顺手写回去"是本项目第一大坑。
2. **不要让 AI 产出直接进 `vault/`** —— 必须 `status=pending`，`review` 级别要人批（ADR-003，本轮已把 `--approve` 的绕过口封上）。
3. **不要把 `vault/` 放进本仓库** —— 哪怕只是"临时测试"（ADR-007）。

还有一条**未决**：`project` 分类在 ADR-003 的信任表里没有归属，当前落 `auto`。这是代码替人做的决定，见 ADR-010，需要所有者二选一。

ADR-011 已采纳（`source_kind` 扩展、`precedence`/`layer` 字段、"Markdown 只是输出侧真相源"），实现见 `adapters/base.py`。

## 8. 下一步

采集、整理（单向）、审核、检索、备份已闭环。**当前真正的拦路虎不是缺功能，是 53 条 `pending` 一条都没批**，导致 MCP 对外恒为空。按优先级：

1. **P4-05 交互式审核**（一屏一条，按键 `批 / 拒 / 改分类 / 跳`，可中断续审）。这是唯一能解开 §1 断点的工单，不做它，后面所有能力都没有用户。
2. **P3-00 fallback 显式化**：`classify` 无信号时返回 `unknown` 而不是静默 `default`。改动小、不依赖 AI、直接消掉 22.6% 的静默误判（ADR-015）。
3. **P3-04 → P3-05 语义去重与冲突检测**：审计数据证明这两项对 AI 的依赖度最高（改错分类 1 秒，判断"是否同一件事"要 1 分钟），应排在 P3-03 的摘要之前。
4. **P3-03 余下部分**：摘要字段（拆分与分类已通）。
5. 可并行的小单：P2-03b WorkBuddy 工作区记忆；Codex 记忆层（sqlite + jsonl，需单独立工单）；阶段 6 打包与 Web UI。
6. 待拍板：LICENSE、ADR-010（`project` 分类信任级）、ADR-015 的三项变更（第 2 项以外）、已知问题 #1 路径脱敏。

**AGENTS 契约已同步**（2026-11-25，经所有者批准）：当前阶段、决策 6 限定输出侧、决策 7 依赖从严、目录职责、测试依赖说明。

## 9. 给接手者的提醒

- 本项目的核心资产是**数据格式与治理规则**，不是代码量。宁可不写功能，不要改字段语义。
- 每次改动跑完两条验收命令再提交，不要攒大批量改动。
- 策划书（`docs/reference/`）是历史输入，不是需求来源。有疑问查 `docs/03-plan-review.md` 的对照表。
- CLI 测试用 `MEMORYHUB_HOME` 隔离配置目录，别让它读到真实 `~/.memoryhub`。
- 测试需要 `pyyaml` + `mcp`（AGENTS.md 已同步）。
- **"Claude" 是两个产品**：Desktop（应用，`%LOCALAPPDATA%\Claude-Data`）与 Code（CLI，`~/.claude`）。本机只装了 Desktop。查上游来源时**不要只看一个路径就下结论** —— 我犯过这个错，见 `docs/04-upstream-formats.md` §3.0。
