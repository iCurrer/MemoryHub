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
| P1-06 | 15 个单元测试 | 完成 |

---

## 阶段 2 · 采集（当前阶段）

### P2-01 迁移 frontmatter 到 PyYAML

- **目标**：支持完整 YAML，保证 `.md` 可被 Obsidian 正确解析。
- **落位**：`src/memoryhub/frontmatter.py`（保留同名函数签名 `split` / `join`，内部换实现）
- **依赖**：无。本工单同时引入项目第一个运行时依赖，需在 `pyproject.toml` 声明 `pyyaml`。
- **完成判据**：
  1. 现有 15 个测试全绿，且 `test_rejects_nested_yaml` 改为断言"嵌套 YAML 可正常解析"。
  2. 新增测试：写出的 `.md` 用 PyYAML 独立 `safe_load` 能解析通过。
  3. 新增测试：含多行字符串、嵌套 `source:` 列表的 frontmatter 可往返。
  4. `docs/01-decisions.md` 的 ADR-005 追加"部分取代"说明。
- **禁止**：不要同时改动目录结构与字段名。这是纯实现替换。

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

### P2-03 Adapter 接口与能力声明

- **目标**：定义外部来源接入协议，后续所有适配器实现它。
- **落位**：新建 `src/memoryhub/adapters/base.py`
- **依赖**：P2-01
- **要点**：
  - `discover() -> list[SourcePath]`：探测可能的来源路径，返回置信度
  - `read(path) -> list[MemoryEntry]`：只读解析，产出 `status=pending`
  - `capabilities`：声明 `can_read` / `can_write`（`can_write` 恒为 `False`，见 ADR-002）
  - 每个 adapter 必须声明 `name` 与 `source_kind` 默认值
- **完成判据**：一个 `DummyAdapter` 的测试可完整跑通 discover → read → 落 inbox。

### P2-04 Codex 适配器

- **落位**：`src/memoryhub/adapters/codex.py`
- **依赖**：P2-03
- **要点**：目标是 `AGENTS.md`（全局 + 项目级）。**必须标记 `source_kind="instruction"`**，不得当作人格记忆。
- **完成判据**：给定一个含多级标题的 `AGENTS.md`，能产出多条 `source_kind=instruction` 的 pending 条目，且 `source_path` / `source_hash` 正确。

### P2-05 Claude Code 适配器

- **落位**：`src/memoryhub/adapters/claude.py`
- **依赖**：P2-03
- **要点**：路径不硬编码。启动时探测 → 让用户确认 → 记住配置。版本差异要降级为"用户手动指定路径"而不是报错退出。
- **完成判据**：路径不存在时给出可操作提示（而不是 traceback）；路径存在时与 P2-04 同标准。

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
| 1 | 自研 frontmatter 不支持嵌套 / 多行 | `.md` 不能被 Obsidian 正确解析 | P2-01 |
| 2 | `doctor` 输出含绝对路径 | 公开仓里贴日志会泄露用户名 | 待定，见 `HANDOFF.md` |
| 3 | 锁为自制 O_EXCL + 超时清陈旧 | 高并发下有竞态，但单用户场景够用 | 阶段 5 评估 |
| 4 | 无 `backup` 命令，Git 需手工 | 备份靠人记得 | P5-04 |
| 5 | 声明 `>=3.11` 但只在 3.13 验证 | 3.11 上 f-string / tomllib 行为未测 | 加 CI 多版本矩阵 |
