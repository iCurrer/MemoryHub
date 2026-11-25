# 开发路线与工单

本文件是**可执行的任务清单**，供人或 AI Agent 直接领取。领取规则：

1. 严格按阶段顺序，不跨阶段并行；阶段内有依赖的按编号顺序。
2. 每个工单完成后必须：补测试 → 跑通验收命令 → 更新 `HANDOFF.md` → 提交一次。
3. 工单描述与 `AGENTS.md` 冲突时，以 `AGENTS.md` 为准并向项目所有者提出。
4. 不得为了"先跑起来"而降低 `AGENTS.md` 里的硬约束。

**阶段门禁**（任一不满足不得进入下一阶段）：

```bash
PYTHONPATH=src python -m unittest discover -s tests -v   # 必须全绿
PYTHONPATH=src python -m memoryhub doctor                 # 必须无 FAIL
```

---

## 阶段 1 · 核心数据层 ✅

已完成，工单留档备查。

| 工单 | 内容 | 状态 |
|---|---|---|
| P1-01 | `MemoryEntry` 领域模型 + 校验 | 完成 |
| P1-02 | 零依赖 frontmatter 读写 | 完成（待 P2-01 迁移） |
| P1-03 | 原子写入 + 跨进程锁 | 完成 |
| P1-04 | inbox ↔ vault 流转，无重复文件 | 完成 |
| P1-05 | CLI 9 命令 | 完成 |
| P1-06 | 15 个单元测试 | 完成（现 38 个） |

---

## 阶段 1.5 · 加固 ✅

评审后发现的静默丢数据 / 失联 / 门禁矛盾，已在进入阶段 2 之前修完。决策记录见 ADR-009。

| 工单 | 内容 | 状态 |
|---|---|---|
| P1.5-01 | id 后缀 4→8 hex；`store.create()` 拒绝 id 复用，不再静默覆盖 | 完成 |
| P1.5-02 | 读失败显式上报：`scan()` 返回 errors，`doctor` 判 FAIL，CLI 汇总告警 | 完成 |
| P1.5-03 | `--vault` 子命令前后都生效（此前前置写法被静默丢弃） | 完成 |
| P1.5-04 | `--approve` 对 `review` 级别需 `--force`，`blocked` 不可越过 | 完成 |
| P1.5-05 | `doctor` 分 FAIL / WARN，`init --no-git` 后门禁可过 | 完成 |
| P1.5-06 | `confidence=low` 归入 `review`（补齐 ADR-003 明文） | 完成 |
| P1.5-07 | 查询默认过滤过期条目，`--include-expired` 打开（ADR-006） | 完成 |
| P1.5-08 | 类型校验（tags/supersedes/时间戳/id），残缺 frontmatter 不再抛 TypeError | 完成 |
| P1.5-09 | 剥离 UTF-8 BOM（Windows 编辑器写入后 frontmatter 会整块失效） | 完成 |
| P1.5-10 | 错误路径不再出栈：缺文件、锁超时、坏 frontmatter 均给可读错误 | 完成 |

---

## 阶段 2 · 采集（当前阶段）

### P2-01 迁移 frontmatter 到 PyYAML ✅

- **目标**：支持完整 YAML，保证 `.md` 可被 Obsidian 正确解析。
- **落位**：`src/memoryhub/frontmatter.py`（保留同名函数签名 `split` / `join`，内部换实现）
- **依赖**：无。本工单同时引入项目第一个运行时依赖，已在 `pyproject.toml` 声明 `pyyaml>=6.0`。
- **完成判据**（全部达成）：
  1. 现有测试全绿，`test_rejects_nested_yaml` → `test_nested_yaml_is_parsed`，断言嵌套 YAML 可正常解析。
  2. 新增测试：写出的 `.md` 用 PyYAML 独立 `safe_load` 能解析通过。
  3. 新增测试：含多行字符串、嵌套 `source:` 列表的 frontmatter 可往返。
  4. 新增测试：Obsidian 块列表可读出；BOM 前缀文件可读出。
  5. `docs/01-decisions.md` 的 ADR-005 已追加"部分取代"说明。
- **禁止**：不要同时改动目录结构与字段名。这是纯实现替换。
- **实际结果**：38 → 45 个测试。三个额外处理记在 ADR-005：YAML 1.2 布尔规则、时间戳还原为字符串、重复键报错。
- ~~**遗留**：`AGENTS.md` 的"测试可在无第三方包的机器上跑通"对阶段 2 不再成立~~ —— **已解决**（2026-11-25，AGENTS.md 已同步）。

### P2-02 SQLite 索引 + FTS5 trigram ✅

- **落位**：`src/memoryhub/storage/index.py`；CLI `index` 命令；`search` 自动使用
- **依赖**：P2-01
- **实际落地**：
  - 表：`entries`（id/category/status/updated_at/path/content_hash/body/tags）、`files`（path/mtime_ns/size）、`meta`（tokenizer/built_at）+ `entries_fts`
  - 分词：优先 `trigram`，装不上才退 `unicode61`（`meta.tokenizer` 记录实际值，`doctor` 会显示）
  - 新鲜度：每轮 `stat` 全部条目文件比对 `files` 表 —— stat 比解析便宜得多，所以每次 `search` 都能承受；任何外部编辑都会被发现并触发重建
  - **1-2 字的中文查询走 `LIKE` 兜底**：trigram 匹配不了 3 字以下，返回空会被误读成"记忆没了"
  - `rebuild()` 用**真实文件路径**写入，不用 `target_path()` 推算（避免 status 与位置不一致时索引记错路径）
  - 索引坏掉时 `search` 回退全表扫描并告警，**索引永远不能把搜索拖死**
- **完成判据**（全部达成，`tests/test_index.py` 9 例）：
  1. 中文短语（≥3 字）可命中 —— `开发项目` / `优先考虑` 均命中。
  2. 删除 `index.db` 后 `search` 自动重建且结果一致 —— 三个查询前后逐一对齐。
  3. `doctor` 增加 index 检查（absent/stale = WARN，broken = FAIL）。
  4. 额外：外部编辑触发重建、新条目被发现、Markdown 仍是唯一真相源。
- **禁止项遵守**：索引没有 Markdown 之外的字段；删库即失忆这件事有测试兜底。

### P2-03 Adapter 接口与能力声明 ✅

- **目标**：定义外部来源接入协议，后续所有适配器实现它。
- **落位**：`src/memoryhub/adapters/base.py`（+ `adapters/__init__.py` 注册表）
- **依赖**：P2-01、ADR-011（已采纳）
- **格式依据**：[`04-upstream-formats.md`](04-upstream-formats.md)。**不要**照着策划书 §4.2 的猜测设计接口
- **实际落地**：
  - `SourcePath(path, adapter, source_kind, layer, precedence, confidence, note)` + `content_hash()`
  - `Adapter.discover() -> list[SourcePath]`、`Adapter.read(source) -> list[MemoryEntry]`
  - `capabilities()` 固定返回 `can_write=False`（ADR-002），测试逐 adapter 校验
  - `read()` 接收 `SourcePath` 而非文本：adapter 可以解析 md / JSONL / SQLite，核心层不需要知道
  - 共享 helper：`strip_quoted_metadata()`（识别 `> key: value` 头部，含 BOM）、`bullets()`（切分记忆原子）
- **完成判据**（全部达成）：
  1. `DummyAdapter` 跑通 discover → read → 落 inbox。
  2. **非 Markdown** 的 `JsonlDummyAdapter` 跑通同一链路（接口没绑死 markdown）。
  3. 同一 adapter 返回的两条来源保序（`precedence` / `layer` 不丢失）。

### P2-03a 首个真实适配器：WorkBuddy ✅

- **落位**：`src/memoryhub/adapters/workbuddy.py` + CLI `memoryhub scan`
- **为什么先做它**（对应 roadmap 末尾"建议调整"）：格式是**纯 Markdown、零解压、零 SQLite**，也是唯一能直接产出"用户画像"类记忆的来源，用最小成本验证整条链路
- **要点**：
  - 路径：`~/.workbuddy/memory/*_memory.md`（`WORKBUDDY_HOME` 可覆盖，测试用）
  - 头部 `> Last updated:` 作为 `created_at`（无时区则按本地时区补齐）；`# 标题` / `> 元数据` 与正文分离
  - 每个顶级 bullet = 一条记忆；`## 小节标题` 变成 tag
  - `source_kind=auto_memory`、`category=profile` → 按 ADR-003 自动进 `review` 档
  - `scan` 用 `(adapter, source_path, body)` 指纹去重，**重复执行不产生重复条目**
  - `--dry-run` 只报数字不落盘
- **完成判据**（全部达成）：
  1. 给定真实格式样本，产出 N 条 `status=pending` / `trust=review` 条目，`source_path` / `source_hash` / `created_at` 正确。
  2. 连续两次 `scan` 第二次为 `0 imported, N already present`。
  3. `--dry-run` 不写任何文件。
  4. 源目录不存在时退出码 0 且不报错。

### P2-03b WorkBuddy 工作区记忆（未做）

发现太晚、当时只适配了一种形态。补记缺口：

- **`user-<uuid>-personal/`**：已补（`IDENTITY.md` / `SOUL.md` / `USER.md` → `profile`；`MEMORY.md` 按 bullet 切 → `auto_memory`）
- **`<workspace>/.workbuddy/memory/`**：`MEMORY.md` + 按天文件 `YYYY-MM-DD.md`，**未适配**。工作区路径不能从 home 推导，需要用户给根目录（或读 `workspace-state.json`，结构未验证）
- 注意：WorkBuddy 的 `MEMORY.md` 是**按标题分组的正文**，Claude 的 `MEMORY.md` 是**链接索引**（`- [Title](file.md)`）。**不要共用解析器。**

**2026-11-24 取消 Cherry Studio 适配**（所有者决定）：该工具的 `SOUL.md` 属于助手而非用户，且本机只有内置 agent、无用户记忆可采。已删除 `adapters/cherry.py`，连同只被它使用的 `adapters/persona.py`。

### P2-04 Codex 适配器 ✅

- **落位**：`src/memoryhub/adapters/codex.py`
- **依赖**：P2-03、ADR-011
- **要点**（按 [`04-upstream-formats.md`](04-upstream-formats.md) §2 修正，原工单描述已过时）：
  - 指令层**不是单个文件**：`$CODEX_HOME/AGENTS.override.md` → `AGENTS.md`（只取第一个非空），再按 git root → cwd 逐层查 `AGENTS.override.md` → `AGENTS.md` → `project_doc_fallback_filenames`，每层最多一个
  - **必须标记 `source_kind="instruction"`**，不得当作人格记忆；并记录 `layer`（global/project/nested）与链中位置
  - 要处理 `project_doc_max_bytes`（默认 32 KiB）的截断：超出部分上游根本看不到，不能假装读到了
  - 记忆层是另一个工单：`memories_1.sqlite`（表 `stage1_outputs`）与 `sessions/**/*.jsonl.zst` 是 `session_distill`，**不在本工单范围**
- **完成判据**：
  1. 给定一个"全局 + 根 + 嵌套 override"的三层目录树，能产出 3 条 `source_kind=instruction` 的 pending 条目，`source_path` / `source_hash` / `layer` / `precedence` 全部正确。
  2. `AGENTS.md` 被 `AGENTS.override.md` 遮蔽时，被遮蔽的那条不产出（或标为 shadowed）。
  3. 超过 `project_doc_max_bytes` 时，明确报"已截断"而不是静默少内容。
  4. 路径不存在时给出可操作提示（而不是 traceback）。
- **实际落地**：dapters/codex.py，7 个用例。遮蔽语义已用真 codex exec 验证（docs/04 §2.1）。
- **已知差异**：单文件超过 project_doc_max_bytes 时我们整份跳过并告警，Codex 会截断取前缀——宁可少导入，不假装读到了。

### P2-05 Claude Code 适配器 ✅

- **落位**：`src/memoryhub/adapters/claude.py`
- **依赖**：P2-03、ADR-011
- **要点**（按 [`04-upstream-formats.md`](04-upstream-formats.md) §3）：
  - 指令层：`~/.claude/CLAUDE.md`（global）/ `./CLAUDE.md` 或 `./.claude/CLAUDE.md`（project）/ `./CLAUDE.local.md`（local）/ `.claude/rules/**/*.md`，`layer` 与 `precedence` 如实记录
  - 记忆层：`~/.claude/projects/<project>/memory/*.md`；**`MEMORY.md` 是索引，跳过**（否则每条记忆进两遍）
  - `type` → `category`：`user`→`profile`、`feedback`→`preference`、`project`→`project`、`reference`→`knowledge`；`modified` → `created_at`
  - **不假设 `AGENTS.md` 生效**：默认 `claude-md-or-agents-md` 下，有 `CLAUDE.md` 就不读 `AGENTS.md`
- **实际落地**：
  - `CLAUDE_HOME` / `CLAUDE_PROJECT_ROOT` 可覆盖，默认 `~/.claude` 与 cwd（在仓库里跑 `scan` 会导入该仓库的 `CLAUDE.md`，这是正确行为，但测试必须隔离）
  - 指令按 1-2 级标题切段，**代码围栏内的 `#` 不算标题**；中文标题的 slug 保留 CJK
  - 超过 4 MiB 的指令文件跳过并告警（Claude 自己也跳过）
- **完成判据**（全部达成）：
  1. 路径不存在时不报错、不 traceback。
  2. `type=feedback` + `modified` 的记忆文件 → `category=preference`、`created_at` 取自 `modified`、`source_kind=auto_memory`。
  3. `MEMORY.md` 不被当作记忆导入。
  4. 指令条目 `source_kind=instruction`、`category=decision` → `trust=review`，且带 `source_hash`。

> **验证状态（2026-11-24 更新）**：CLI v2.1.331 装好后复验 —— **指令层已真机验证**：`~/.claude/CLAUDE.md`（2 节）与项目 `./CLAUDE.md`（4 节）分别以 global/0、project/10 入队。
>
> **auto memory 层仍未验证**：`projects/<project>/memory/` 目录存在但为空（未 `/login`，无法触发写入）。`type` / `modified` 的映射仍只有官方文档支撑，首次产生真实记忆文件后请复核 `docs/04` §3.3。

### P2-06 文件监控 + 防抖 + 哈希去重 ✅

- **落位**：`src/memoryhub/watcher/`（`core.py` 规则 / `poll.py` 循环）+ CLI `watch`
- **依赖**：P2-04、P2-05
- **实现选择（与工单不同，需知悉）**：**没有引入 `watchdog`**，用 2 秒轮询 + 防抖。
  理由：记忆文件的变化尺度是分钟级，`watchdog` 只买到更低的延迟，却把平台观察器带进测试；
  轮询让三条判据可以用**注入时钟**验证，且不新增依赖。将来要换，只替换事件来源，
  `core.py` 的规则不动。
- **要点**：
  - 三层判定：`MtimeTracker`（要不要进防抖队列）→ `Debouncer`（静默够了没）→ `HashTracker`（内容真变了没）
  - `exports/` 与 `logs/` 一律排除（ADR-002 降级后唯一的循环风险点）
  - `scan` 与 `watch` 共用 `import_entries()`，去重逻辑只有一份
- **完成判据**（全部达成，`tests/test_watcher.py` 11 例）：
  1. 连续快速写入同一文件只产生一个任务 —— 注入时钟验证 t=0/1/2 三次写入只释放一次。
  2. 内容不变的重写不产生任务 —— 同内容重写 hash 不变即丢弃。
  3. 写入 `exports/` 不触发导入 —— `is_excluded` 覆盖 exports 与 logs。
  4. 额外：进程重启不重复导入（vault 指纹集兜底）；防抖窗口内的二次修改不会漏。
- **踩到的真 bug**：初版每轮轮询都对所有文件 `observe`，静默计时器被无限重置 → 永不释放。
  这正是"只在 mtime 变化时入队"这层存在的理由，已加注释与测试。

### P2-07 周期校准 —— 已由 P2-06 的轮询本体覆盖

- 原计划是 Watchdog 事件 + 周期扫描兜底。改成轮询后，**每一轮本身就是一次校准**，
  不存在"事件通道漏了要靠扫描补"的缝隙。保留此条为回归项：若将来引入 `watchdog`，
  必须同时恢复独立的周期校准（`--interval` 调大即可），并验证停掉事件通道后仍能发现变更。

---

### P2-08 条目粒度 + 分类归属 ✅

- **落位**：`adapters/base.py`（`split_oversized` / `drop_heading_only`）、`classify.py`、CLI `recategorize` / `scan --refresh`、`doctor` 的 `mcp visibility`
- **动因**：ADR-014。首次导入的 42 条中位数 90 字、最长 1276 字，且 **100% 是 `profile`**，导致审核无意义、MCP 查不到任何东西
- **实际落地**：
  - 单条上限 400 字；超出按子标题 → 空行段落 → 句子三级回退；父标题并入首个子条目，不产生空条目
  - 分类改为加权打分，权重 3 决定性（`github.com` / `绝不` / `铁律`…）；同分按 decision > profile > preference > project
  - `recategorize` 默认 dry-run，打印「原 → 新 + 命中词与权重」；`--apply` 才写
  - `scan --refresh` 把来源不再产生的 pending 条目转 `archived`（可恢复），避免一次切分留下两代条目
- **实测**（`D:/my-memory`）：42 → 59 条（53 pending + 6 archived）；最长 1276 → 401 字；`profile` 占比 100% → 40%；分类 decision 21 / profile 21 / preference 10 / project 7
- **完成判据**（全部达成，`tests/test_classify.py` 17 例 + `tests/test_migrate.py` 9 例）：
  1. 长条目切分后无空条目、无超过上限的碎片
  2. 含 `github.com` 的段落即使同时含"必须/不得"仍判为 `project`
  3. `recategorize` 无 `--apply` 时不改任何文件
  4. `scan --refresh` 归档被取代的旧条目，`--dry-run` 下不归档

---

## 阶段 3 · AI 整理（仅产出提案）

> **2026-11-25 调整**：审计实测规则分类整体 52.8%（`docs/04-classify-audit.md`、ADR-015），
> 「AI 可延后」的前提不再成立。新增 P3-00，并把 P3-03 的范围从「全量分类」收窄到「规则自认没把握的部分」。
> 顺序也从「分类优先」改为「拆分优先」——7 条 mixed 证明切不细就必错。

> **2026-11-25 进度**：P3-01 ✅ / P3-02 ✅（改手写校验，未引入 Pydantic）/ P3-03 ⚠️ 部分
> （拆分与分类已通，摘要字段未做）/ P3-05 ✅（判定层当天重做）/ P3-00 ⬜ 未做 / P3-04 ⬜。
> 实现说明见 ADR-016、ADR-018；验收为 343 个测试 + 两处本地假服务端端到端（真实 HTTP）。
> **P3-00 仍未做**：AI 模式下不走 `classify`，但默认路径（不带 `--ai`）仍受 fallback 影响，
> 所以它没被 AI 层替代，只是不再阻塞主流程。

| 工单 | 内容 | 落位 | 完成判据 |
|---|---|---|---|
| P3-00 | `classify` 无信号时返回 `unknown` 而非静默返回 `default`；`unknown` 强制进判定队列 | `classify.py` / `cli.py` | 审计脚本「no signal」桶从 7.7% 蒙对变为显式 `unknown`；22.6% 的静默误判消失。**不依赖 AI，可先做** |
| P3-01 | Provider 抽象（OpenAI 兼容 / 自定义 Base URL），未配置时整体跳过 | `ai/provider.py` | 无 API 时全部功能仍可用（回归测试） |
| P3-02 | 结构化输出校验（不引入 Pydantic，保持零第三方依赖，见 ADR-005） | `ai/schema.py` | 返回非法 JSON / 缺字段时被拒绝且给出原因 |
| P3-03 | **拆分优先**：先判定「这段里有几个独立事实」并给出切分建议；再对规则覆盖不到的条目（fallback + keyword 两类，实测 35.8%）做分类与摘要 | `ai/split.py` / `ai/classify.py` | 切分建议不自动改文件，只产提案；分类产出 `status` 恒为 `pending` |
| P3-04 | 语义去重 | `ai/dedupe.py` | 近似条目产出合并建议，不自动合并 |
| P3-05 | 冲突检测与取代链维护 ✅ | `ai/claims.py` / `ai/conflict.py` / `conflicts.py` | 矛盾条目产出 `conflicts/` 记录 + 建议 `supersedes`，**不自动写入**。2026-11-25 真实模型实测不合格，见 ADR-018 末节与下方「P3-05 实际落地」 |

**阶段 3 硬约束**：AI 产出的一切写入 `inbox/`，`status=pending`，绝不直接落 `vault/`。

**阶段 3 验收基线**：P3-03 落地后用 `python tools/audit_classify.py --vault <vault> --min-accuracy 0.85`
复测。这是本阶段唯一可量化的成败判据——没有它，"AI 比规则好"只是又一次判断。

**关于 P3-02 的实现方式**：原 `Pydantic 校验` 与 ADR-005「阶段 1 零第三方运行时依赖」冲突，改为手写 schema 校验。
若确实需要 Pydantic，应先提 ADR 推翻 ADR-005，而不是顺手加依赖。

**P5-06 实际落地（2026-11-25，ADR-019）**：`memoryhub sync [--to ...] [--write] [--diff] [--list]`。

- 只写 `<!-- MEMORYHUB:BEGIN -->` / `<!-- MEMORYHUB:END -->` 之间，区块外一个字节不碰；删掉区块即完全还原。
- 三个目标：`~/.claude/CLAUDE.md`、`~/.workbuddy/user-*/MEMORY.md`、`~/.codex/AGENTS.md`（缺失则新建）。
  路径与 adapters 共用 `CLAUDE_HOME` / `CODEX_HOME` / `WORKBUDDY_HOME` —— 读哪写哪。
- 默认 dry run；`--write` 落盘，写前 `*.bak-memoryhub-<时间戳>` 备份，写后回读校验。
- **三个适配器在 read 时剥掉区块**，否则每次同步都会成为下次导入的输入。真实库实测：写完后
  `import` 得 0 new（25 / 17 / 0 already present），待审队列没增加。

**P3-05 判定层重做（2026-11-25 第二轮）**：`ai/claims.py`（切片段 + 配对）把比较单位从条目降到片段，
`ai/conflict.py` 改成**一次调用只判一对**。关键实测：

- 只共享高频词的那一对（论文题目未定 vs 已定）按 IDF Dice 排 4,803 个片段对的第 207 名。
  **加一条「开头三字相同」的主题锚点通道后升到第 18 名**——这是唯一能召回它的通道。
- 该对第一次被判 `none`（"一个是题目一个是流程状态"）；提示词补上「状态与结论是同一件事」后
  判为 `contradicts`，理由准确。**这是规则缺口，不是模型能力问题。**
- `contradict` 少写一个 s 会被判无效 —— 已加别名表。校验器不该制造假阴性。
- 真实库产出 24 条提案（22 重复 / 2 矛盾），全部 `verified=true`；抽查的 5 条「重复」全是真重复，
  且 `scope=partial` 正确阻止了「整条归档」。
- **未解决**：4,803 段对只送审 120 段（2.5%）。`stats` 把每个数字都打出来，`--max-pairs` 可扩大。
  任何「没有冲突」只对送审那批成立。

**P3-05 实际落地（2026-11-25，ADR-018）**：`ai/conflict.py`（判定）+ `conflicts.py`（提案与唯一写入口）。

- 输入是**全部**已批准未过期条目，单次 ≤300 条，超限拒绝而非截断。不做相似度预筛 —— 实测真实的
  「论文题目未定 vs 已定」那对在同分类 309 对里按字符 bigram 余弦只排第 31（0.052），阈值化必漏。
- 每条判定必须带两侧逐字引用；定位不到就把关系降级为 `contradicts` 并清空 `winner`（保留 pair）。
  **判定了什么是"人在审"，判不了"谁对"不许假装。**
- 提案落 `inbox/conflicts/*.json`（该目录被 store 扫描，写 `.md` 会被当成坏条目，`doctor` 变红）。
- `memoryhub conflicts` 列表 / `--show <id>` 详情 / `--ai` 生成；`memoryhub supersede <old> --by <new>`
  是唯一的写入路径（四道闸 + 先写 `supersedes` 后封窗 + 结案提案移 `logs/conflicts/`）。
- 已知缺口：归档对整条生效，混装条目（如真库里那条 401 字的）需要先拆分再取代；`supersede` 会警告但不代做。

**与 P3-04 的边界**：`duplicate`（同一件事两份）与 `supersedes`（同一件事的新旧两份）由同一个判定给出，
去重落地时必须复用这条判定，不要另起一套，否则会出现两条互斥的合并路径。

---

## 阶段 4 · 审核流（CLI）

| 工单 | 内容 | 完成判据 |
|---|---|---|
| P4-01 | 差异预览：`memoryhub diff <id>` 对比提案与现有条目 | ✅ 完成 |
| P4-02 | 批量操作：`inbox list --auto` / 批量 approve | ✅ 完成（`approve --all` + `--category` / `--tag` / `--dry-run`） |
| P4-03 | 变更摘要：`memoryhub log --since 7d` | ✅ 完成 |
| P4-05 | 交互式审核：`memoryhub review`，一屏一条 | ✅ 完成（**2026-11-25 新增工单**，见下） |

**P4-01 实际落地**：`diff <id>` 默认自动配对 —— 取**同一 `source_path` 的最新已批准条目**作为对比对象，
输出元信息（状态/类别/信任/来源/时间）+ 统一 diff；`--against <id>` 可显式指定；无配对时打印正文并说明原因。

**P4-02 实际落地**：`approve --all`，可按 `--category` / `--tag` 过滤，`--dry-run` 只列出。
**blocked 永远跳过**（打印到 stderr），不因为批量操作就绕过 ADR-003。

**P4-05 实际落地**：`src/memoryhub/review.py` + `memoryhub review`（**2026-11-25 新增**，原路线图没有这张工单）。

- 起因是实测断点：53 条 `pending`、`approved` 为 0，MCP 对外恒为空。瓶颈不是缺能力，是
  `approve` 要求你先知道 id —— 读列表、滚动、复制 32 位标识符，重复 53 次。每一步都是"改天再说"的理由。
- 一屏一条，按键 `a` 批 / `r` 拒 / `c` 改分类 / `s` 跳过 / `q` 退出。改分类后**同一条重新显示**再决策，
  避免"改了但没看清就批"。
- **队列每次从 vault 重新推导，只有"跳过的 id"被记住**（ADR-017）。存整条队列会让过期状态文件
  有能力藏掉提案或复活已删除的条目，而可推导的东西不该存两份。
- 跳过状态放在配置目录（`~/.memoryhub/review.json`），**不放进 vault** —— vault 里只应有记忆，
  且 cursor 文件会在 `git commit` 里变成 diff。
- `blocked` 条目在界面上直接标明"改分类也解除不了"，只允许 `r`/`s`，不让用户白点一次 `a`。
- 非 TTY 一律拒绝（exit 2）并指向 `approve <id>`：没有终端的循环会读到 EOF，什么也没决定，却看起来成功了。
- 退出时打印本次操作的 id 清单，误按可用 `memoryhub reject <id>` 撤销。

**P4-03 实际落地**：`src/memoryhub/backup/history.py` + `memoryhub log`。
- 历史来源是 **vault 自己的 git log**，不另建变更日志 —— `git log` 就是 ADR-003 依赖的回滚依据，
  另建一份必然与它不一致；也避免在 vault 里多出一个"不是记忆"的文件。
- `--since` 支持 `7d` / `12h` / `2w` / ISO 日期，翻译成 git 的 `--since`。
- `--no-renames`：git 的 rename 状态会把新旧两个路径塞进一行，解析会错位；审核回看不需要该区分。
- 没有提交或不是 git 仓时**明确报错**并给出下一步，而不是返回空列表假装"没有变化"。

**顺带修掉一个真隐患**：`new` / `list` 等命令此前不检查 vault 是否存在，config 未写入时会
静默写到硬编码的 `D:/my-memory`。一次测试运行真的把数据写进了那里。现在 `_store()` 直接报错并
提示 `init`，有测试覆盖。

**同批修复（P4-03 之后）**：`save()` 增加乐观并发校验（`StaleEntryError`）。记忆对象记住加载时的
`updated_at`（`_base_updated_at`，不序列化），写入前与磁盘比对，不一致即拒绝并提示 `store.get()`
重新加载。根因是"status 与文件位置是两个真相源"：保存过期快照会把已批准条目搬回 `inbox/pending`。
`set_status` / `stamp_validity` 内部重新加载，因此不受影响。**已知局限**：时间戳为秒级，
同一秒内的并发写入检测不到。

---

## 阶段 5 · MCP 与备份

| 工单 | 内容 | 完成判据 |
|---|---|---|
| P5-01 | MCP stdio：`memory_search` / `memory_get` / `memory_list` / `memory_propose` / `memory_status` | 任一 MCP 客户端可调用，stdout 只输出协议消息 |
| P5-02 | 权限分级 Read / Propose / Admin | ✅ 完成 |

**P5-03 首刀（2026-11-24，实际使用时补）**：真实导入暴露了两件事 —— ① 适配器无法判断"什么是隐私"，
`MEMORY.md` 里的住址/籍贯/学校被当作 `normal` 导入；② **没有任何命令能改 sensitivity**，
于是 ADR-003 那句"先降级敏感标记再批准"走不通。已补 `memoryhub sensitivity <id> --level`：
改级别后重新推导 trust，可升可降；对已批准条目升到 blocked 会被拒绝并提示先 reject。

**P5-02 实际落地**：`memoryhub mcp --permission read|propose|admin`（或 `$MEMORYHUB_MCP_PERMISSION`）。

| 级别 | 暴露的工具 |
|---|---|
| `read` | `memory_search` / `memory_get` / `memory_list` |
| `propose`（默认） | 上述 + `memory_propose` |
| `admin` | 上述 + `memory_status` |

- 权限**按进程划分，不按客户端身份**：MCP 的 `clientInfo` 是客户端自报且无认证的，
  "Cursor 给 propose、Claude 给 read" 只是安全剧场。stdio 下唯一可信的边界是启动者。
- 未被授予的工具**根本不注册**，所以 `tools/list` 就是真实能力，客户端不会看到一个调不通的工具。
- **任何级别都没有 `memory_write`**：`propose` 只能进待审队列，`admin` 也只是多一个只读的 `memory_status`。
| P5-03 | 敏感信息检测与排除规则 | ✅ 完成（两刀） |

**第一刀（密钥）**：`safety.py` 认 6 类凭证形状；`memory_propose` 命中即 `secret` → blocked。

**第二刀（PII，真实使用后补）**：真实导入暴露了更严重的问题 —— `MEMORY.md` 里的住址/籍贯/学校
全部以 `normal` 落地，一旦批准就会经 MCP 流出。新增分级：

| 级别 | 触发 | 后果 |
|---|---|---|
| `secret` | 凭证形状 | `blocked`，不可批准 |
| `sensitive` | 身份证 / 手机号 / 银行卡 / 完整住址 | `blocked`，需人工降级 |
| `personal` | 城市、区县、学校等弱信号 | 仅标记，不阻断 |

**刻意分强弱**：把每个城市名都当敏感会让"在上海读书四年"被拦，规则一旦频繁误报就会被无视，
连真信号一起失效。

`memoryhub audit [--apply]` 用同一套分类器复查**已有**条目的 body，报告等级偏低者并可批量提升；
已批准的条目不会被静默改级，会提示先 reject/archive。

**真机结果**：42 条里 10 条偏低（1 条完整住址 → blocked，9 条学校/籍贯 → personal）。
| P5-04 | `memoryhub backup`：本地 commit + 可选远端 | ✅ 完成 |
| P5-06 | `memoryhub sync`：把库投影回各 AI 的受管区块 | ✅ 完成（ADR-019） |
| P5-07 | 读取日志 + 三个上下文捷径工具 | ✅ 完成（ADR-021） |

**P5-07 实际落地（2026-11-25，ADR-021）**：`logs/access.jsonl` 记录每次 MCP 读调用，
`memoryhub access [--entry ID]` 读它；标签来自 `--label`（写进客户端配置），不读 MCP 自报的
`clientInfo`；**不记查询词**。同时补 `memory_profile` / `memory_project_context` /
`memory_recent_decisions` —— 「自己调三次 list 再合并」是 AI 一定会做错的指令。
这一条是外部 2.0 方案里唯一说对的新东西；它的其余建议（常驻 daemon、Tauri + React、
「修复」两个不存在的 bug）见 ADR-021 的否决理由。

**P5-04 实际落地**：`memoryhub backup [--remote NAME] [--allow-sensitive]`。

两条规则（写进 `backup/git.py` 的模块 docstring）：

1. **本地提交永远安全，推送不是。** 所以提交总是发生，安全闸只装在推送路径上 ——
   拒绝提交只会丢历史，不会更安全。
2. **闸门看内容，不看意图。** 只要库里有 `sensitive` / `secret`（因而 `blocked`）的条目，
   `--remote` 就拒绝并列出 id；`--allow-sensitive` 是给"已经想清楚并接受代价"的人用的，不是默认。

附带：`backup` 会自动补 `.gitignore`（`database/`、`logs/`、临时文件、锁文件），
且**不覆盖用户已有内容**；派生状态（索引里存着每条记忆的全文）因此不会被提交。

真机验证：本地提交 `061bb4d`（42 条）；`--remote origin` 被拒，列出那条 sensitive 条目。

**P5-01 进度：4/5 完成（2026-11-24）**

- ✅ `memory_search` / `memory_get` / `memory_list`，`memoryhub mcp` 启动 stdio 服务
- ✅ 可见性规则见 ADR-012：`search`/`list` 只见 `approved` 且未过期；`get` 可查任意状态并如实标注
- ✅ 每条结果带 provenance 与 validity
- ✅ 集成测试用真 subprocess 跑完整握手，断言 stdout 只有 JSON-RPC
- ✅ `memory_propose`（2026-11-24 提前）：只产生 `pending`，命中密钥形状即 `blocked`。
  动因见 ADR-012 追加段——云端记忆采不到，只能让客户端自己交
- ⬜ `memory_status` —— 属 P5-02 权限分级的一部分，暂缓

---

### P5-05 两仓边界门禁（代码仓永不接收记忆）✅

- **落位**：`src/memoryhub/boundary.py`、`tools/lint_staged.py`、`tools/hooks/{pre-commit,commit-msg}`、`tools/setup_dev.sh`
- **动因**：ADR-013。此前两次真实泄露（文件里的 profile UUID、**提交信息里**的真实住址），且验证了 `force-push` 不删除 GitHub 上的不可达对象
- **实际落地**：
  - `boundary.violations()`：vault 位于任何 git 工作树内即拒绝，`init` 退出码 2，无 `--force`
  - `init` 往 vault 写 `pre-push` 钩子：远端 URL 含 memoryhub 字样即拒推（含 `$2` 参数优先，避免读到旧配置）
  - `doctor` 新增 `repo isolation` / `vault remote` 两条硬检查
  - `lint_staged.py`：7 条规则（地址 / 家目录 / UUID / 手机号 / 身份证 / 密钥 / 真实校名），扫描暂存文件**与提交信息**；测试夹具用 `# pii:synthetic` 逐行豁免
- **完成判据**（全部达成，`tests/test_boundary.py` 15 例）：
  1. vault 在 git 工作树内 → 拒绝；独立目录或自身为仓 → 通过
  2. `pre-push` 钩子对代码仓 URL 返回 1、对私有 URL 返回 0
  3. 提交信息含地址 / UUID → 拦截；正常信息 → 放行
  4. `lint_staged.py --all` 在本仓库当前状态返回 0

---

## 阶段 6 · 产品化

| 工单 | 内容 | 前置条件 |
|---|---|---|
| P6-01 | PyInstaller 打包（Windows x64） | 阶段 5 完成 |
| P6-02 | 开机常驻 + 系统托盘 | 打包可用 |
| P6-03 | 基础 Web UI（只读总览 + 待审列表） | ✅ 提前落地，见下方说明与 ADR-020 |

**P6-03 提前落地的原因（2026-11-25，ADR-020）**：原来写的门禁是「作者自用满一个月且仍想打开」，这是一条循环论证——没有低摩擦入口就不会长出「仍想打开」的习惯，门禁永远不会满足。真正的信号是**积压是否在增长**：P3-05 当天产出 24 条提案，全部带可定位证据，然后一条也没人处理，因为读它等于 `cat` 一堆 JSON。

已落地：`memoryhub serve [--port N] [--read-only] [--no-browser]`，标准库 `http.server` + 单文件 HTML，零新依赖。只绑 `127.0.0.1`、校验 `Host` 头、每次启动一枚随机 token（走 header，不用 cookie）。三个视图：待审（批准/拒绝/改分类）、冲突提案（两侧并排，点一侧即保留它）、回写计划（只读，`--write` 仍在命令行）。写操作调的是 CLI 同一批函数，`blocked` 与取代链的四道闸因此自动生效。

**没做**：编辑正文、全库检索、批量操作、`sync --write` 按钮。前三项在命令行一眼能做完，第四项会改别的工具每次会话都要加载的文件。

**下一步回到阶段 3**：`782bcbcf` 那条 401 字的混装条目仍未拆（页面对它打了 crowded 警告，但拆条目不代做），P3-00 fallback 与 P3-04 语义去重仍在队列里。

---

## 已知技术债

| # | 债务 | 影响 | 计划 |
|---|---|---|---|
| 1 | ~~自研 frontmatter 不支持嵌套 / 多行~~ **已清（P2-01）** | 用户手改（Obsidian 块列表）导致该条读不出来 | 完成，见 ADR-005 部分取代 |
| 2 | `doctor` 输出含绝对路径 | 公开仓里贴日志会泄露用户名 | 待定，见 `HANDOFF.md` |
| 3 | 锁为自制 O_EXCL + 超时清陈旧 | 高并发下有竞态，但单用户场景够用 | 阶段 5 评估 |
| 4 | 无 `backup` 命令，Git 需手工 | 备份靠人记得 | P5-04 |
| 5 | 声明 `>=3.11` 但只在 3.12/3.13 验证 | 3.11 上 `tomllib` / `datetime.fromisoformat` 行为未测 | 加 CI 多版本矩阵 |
| 6 | `config.save()` 整份重写 | 阶段 3 加 `ai.*` 后，`init` 会抹掉它们 | 阶段 3 前改成合并写入 |
| 7 | `supersedes` 不做存在性校验，可悬空 | 取代链是核心资产却无完整性检查 | 阶段 3 |
| 8 | `status` 与文件位置是两个真相源 | 手改 `status` 不移动文件，出现"躺在 inbox 里的 approved" | 阶段 4 |

（已清：`doctor` 对条目做两次遍历 —— 现在统一走 `store.scan()`。）

---

## 建议调整（已获批准 2026-11-24，部分已执行）

评审认为原编号顺序是"技术债优先"而非"闭环优先"。所有者已批准调整，执行情况：

```
P2-01 (PyYAML)            ✅ 完成
P2-03 (Adapter 接口)      ✅ 完成
P2-03a (WorkBuddy)        ✅ 完成 —— 首个真实来源，闭环已通
P2-02 (SQLite/FTS5)       ⬜ 待做（可随时开始，无决策依赖）
P2-04 (Codex 指令链)      ⬜ 待做
P2-05 (Claude Code)       ⬜ 待做
P2-06/07 (监控 + 校准)    ⬜ 待做
P5-01 只读三件套 (MCP)    ⬜ 建议提前 —— 让别的 AI 真的能读到中央库
```

三条理由（保留原文备查）：

1. P2-02 的价值取决于数据量；实测 400 条检索 114ms，撑到阶段 3 没问题。
2. 策划书 §12.3 自己写的"首个可演示闭环"是「导入→提案→批准→写入→导出→commit」。
3. **P5-03（敏感检测）建议前移到 P3-01 之前**：ADR-003 的 `blocked` 完全依赖自报 `sensitivity`，阶段 3 一开 `auto` 自动入库，密钥可能先进 `vault/`，而检测排在阶段 5 —— 顺序是倒的。

另建议增加一类"使用型工单"（如：接入第一个真实来源后连续自用 7 天并记录审核次数）。
现在 30 个工单全是功能型，而 `docs/00-positioning.md` 的成功判据是"连续使用 30 天且停用会不便"，**没有任何工单服务这个判据**。
