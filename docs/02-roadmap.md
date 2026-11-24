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
- **遗留**：`AGENTS.md` 的"测试可在无第三方包的机器上跑通"对阶段 2 不再成立，待所有者修订措辞。

### P2-02 SQLite 索引 + FTS5 trigram

- **目标**：`search` 从目录扫描改为索引查询，中文可检索。
- **落位**：新建 `src/memoryhub/storage/index.py`
- **依赖**：P2-01
- **要点**：
  - 建表记录 `id / category / status / updated_at / path / content_hash`
  - FTS5 使用 `tokenize='trigram'`（默认 `unicode61` 对中文无效，见 ADR 与 `docs/03-plan-review.md` 第 17 条）
  - `rebuild()` 能从 `vault/` 全量重建；删除 `database/index.db` 后 `search` 仍可工作（自动重建）
- **完成判据**：
  1. 中文短语（≥3 字）可命中；新增对应测试。
  2. 删除索引文件后调用 `search` 自动重建且结果一致。
  3. `doctor` 增加"索引与 Markdown 一致性"检查项。
- **禁止**：索引成为唯一数据源；任何字段只存索引不存 Markdown。

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

### P2-04 Codex 适配器

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

### P2-05 Claude Code 适配器

- **落位**：`src/memoryhub/adapters/claude.py`
- **依赖**：P2-03、ADR-011
- **要点**（按 [`04-upstream-formats.md`](04-upstream-formats.md) §3）：
  - 指令层：`~/.claude/CLAUDE.md`、`./CLAUDE.md`、`./.claude/CLAUDE.md`、`./CLAUDE.local.md`、`.claude/rules/*.md`（可带 `paths:` frontmatter）
  - 记忆层：`~/.claude/projects/<project>/memory/`，`MEMORY.md` 是索引，主题文件是单条记忆
  - **映射已有元数据**：frontmatter 的 `type`（`user`/`feedback`/`project`/`reference`）→ `category`，`modified` → `updated_at`，不要丢弃
  - **不要假设 `AGENTS.md` 生效**：默认 `claude-md-or-agents-md` 下，有 `CLAUDE.md` 就不读 `AGENTS.md`
  - 路径不硬编码，启动时探测 → 让用户确认 → 记住配置；版本差异降级为"用户手动指定路径"而不是报错退出
- **完成判据**：
  1. 路径不存在时给出可操作提示（而不是 traceback）；路径存在时与 P2-04 同标准。
  2. 给定一个 `memory/` 目录（`MEMORY.md` + 2 个带 `type` 的主题文件），能产出 2 条 `source_kind=auto_memory` 的 pending 条目，`category` 与 `updated_at` 来自 frontmatter 而不是默认值。
  3. `MEMORY.md` 的 200 行 / 25KB 加载上限被如实反映（超出部分标为未加载，不假装读到了）。

### P2-06 文件监控 + 防抖 + 哈希去重

- **落位**：`src/memoryhub/watcher/`
- **依赖**：P2-04、P2-05
- **要点**：
  - 防抖窗口 2-5 秒，可配置
  - 事件入队后先算 SHA-256，与上次快照一致则丢弃
  - 识别自身 `exports/` 目录输出，不重复导入（ADR-002 降级后的唯一循环风险点）
- **完成判据**：
  1. 连续快速写入同一文件只产生一个任务。
  2. 内容不变的重写不产生任务。
  3. 写入 `exports/` 不触发导入。

### P2-07 周期校准

- **落位**：`src/memoryhub/watcher/calibrate.py`
- **依赖**：P2-06
- **要点**：每 5-15 分钟轻量扫描已配置来源，用哈希补齐漏掉的事件。
- **完成判据**：手动停掉事件通道后修改文件，下次校准能发现。

---

## 阶段 3 · AI 整理（仅产出提案）

| 工单 | 内容 | 落位 | 完成判据 |
|---|---|---|---|
| P3-01 | Provider 抽象（OpenAI 兼容 / 自定义 Base URL），未配置时整体跳过 | `ai/provider.py` | 无 API 时全部功能仍可用（回归测试） |
| P3-02 | Pydantic 校验 AI 结构化输出 | `ai/schema.py` | 返回非法 JSON / 缺字段时被拒绝且给出原因 |
| P3-03 | 分类与摘要 | `ai/classify.py` | 产出 `category` 与摘要，`status` 恒为 `pending` |
| P3-04 | 语义去重 | `ai/dedupe.py` | 近似条目产出合并建议，不自动合并 |
| P3-05 | 冲突检测与取代链维护 | `ai/conflict.py` | 矛盾条目产出 `conflicts/` 记录 + 建议 `supersedes`，**不自动写入** |

**阶段 3 硬约束**：AI 产出的一切写入 `inbox/`，`status=pending`，绝不直接落 `vault/`。

---

## 阶段 4 · 审核流（CLI）

| 工单 | 内容 | 完成判据 |
|---|---|---|
| P4-01 | 差异预览：`memoryhub diff <id>` 对比提案与现有条目 | 输出可读 diff，标出来源与时间 |
| P4-02 | 批量操作：`inbox list --auto` / 批量 approve | 支持按 category 批批，不逐条点 |
| P4-03 | 变更摘要：`memoryhub log --since 7d` | 用于每周回看（补 ADR-003 的代价项） |

---

## 阶段 5 · MCP 与备份

| 工单 | 内容 | 完成判据 |
|---|---|---|
| P5-01 | MCP stdio：`memory_search` / `memory_get` / `memory_list` / `memory_propose` / `memory_status` | 任一 MCP 客户端可调用，stdout 只输出协议消息 |
| P5-02 | 权限分级 Read / Propose / Admin | 默认无 `memory_write`；不提供无审核写入 |
| P5-03 | 敏感信息检测与排除规则 | 明显密钥被阻断，`doctor` 有对应检查 |
| P5-04 | `memoryhub backup`：本地 commit + 可选远端 | 远端不可用时本地提交成功、任务进重试 |

**P5-01 进度：3/5 完成（2026-11-24）**

- ✅ `memory_search` / `memory_get` / `memory_list`，`memoryhub mcp` 启动 stdio 服务
- ✅ 可见性规则见 ADR-012：`search`/`list` 只见 `approved` 且未过期；`get` 可查任意状态并如实标注
- ✅ 每条结果带 provenance 与 validity
- ✅ 集成测试用真 subprocess 跑完整握手，断言 stdout 只有 JSON-RPC
- ⬜ `memory_propose` / `memory_status` —— 等 P5-02 的权限分级，否则等于开放无审核写入

---

## 阶段 6 · 产品化

| 工单 | 内容 | 前置条件 |
|---|---|---|
| P6-01 | PyInstaller 打包（Windows x64） | 阶段 5 完成 |
| P6-02 | 开机常驻 + 系统托盘 | 打包可用 |
| P6-03 | 基础 Web UI（只读总览 + 待审列表） | **作者自用满一个月且仍想打开** |

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
