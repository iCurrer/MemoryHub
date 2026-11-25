# MemoryHub

跨 AI 工具的个人记忆治理层。本地优先、可审计、可迁移。

> **状态：阶段 1 / 1.5 / 2 / 4 / 5 完成（含回写分发），阶段 3 部分完成（AI 整理已通、冲突判定层待重做），阶段 6 未开始。尚未发布。**
> 现在可以：从三个来源采记忆 → 人工审核 → 落 Markdown 库 → 检出互相矛盾的条目并建立取代链 → 通过 CLI 或 MCP 检索。
> 语义去重、摘要字段与 Web 界面**还没有**。请勿当作日常工具依赖。

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

- **来源留痕**：每条记忆能追到哪个工具、哪个文件、哪次哈希、在指令链的第几层。
- **时效裁决**：`valid_from` / `valid_until` / `supersedes`，旧结论不删除而是封窗，可查"当时为什么这么定"。
- **分级信任**：不是所有记忆都配得上人工逐条审核，但所有记忆都必须可回滚。

（竞品信息基于 2026 年 11 月的公开资料，正式对外表述前需再次核实版本与能力边界。）

## 当前已实现

**数据层**

- `MemoryEntry` 领域模型：分类、状态、信任级、敏感级、来源、时效、`precedence` / `layer`
- 完整 YAML frontmatter 读写（PyYAML）：嵌套、多行、Obsidian 块列表、UTF-8 BOM 都能往返
- 原子写入 + 跨进程文件锁；一个 id 只对应一个文件；**id 复用会被拒绝而不是覆盖**
- `inbox`（待审）与 `vault`（正式）之间的流转，不留重复文件
- 三级信任：`auto` / `review` / `blocked`；`review` 直接批准需显式 `--force`，敏感条目禁止批准
- 时效裁决：`valid_until` 已过期的条目默认不出现在 `list` / `search` 里（`--include-expired` 打开）
- 读不了的 `.md` 不会被静默跳过：`list` / `search` 告警，`stats` 计数，`doctor` 判 FAIL

**采集（3 个适配器，全部只读）**

| 适配器 | 读什么 |
|---|---|
| `workbuddy` | `~/.workbuddy/memory/*_memory.md`、`user-*-personal/{IDENTITY,SOUL,USER,MEMORY}.md` |
| `claude` | `~/.claude/CLAUDE.md`（用户档案）、`./CLAUDE.md`、`./CLAUDE.local.md`、`.claude/rules/*.md` |
| `codex` | `AGENTS.md` 指令链：全局 → 项目根 → cwd，`AGENTS.override.md` 遮蔽同目录 `AGENTS.md` |

- `scan`：一次性采集，按内容指纹去重，`--dry-run` 可预览；`--refresh` 把来源不再产生的 pending 条目转 `archived`（重扫后不留两代条目）
- `watch`：2 秒轮询 + 防抖 + 哈希，只导入真正变化的文件（`--once` 供 cron/测试）
- **粒度控制**：单条上限 400 字，超出按「子标题 → 空段落 → 句子」切分；父标题并入首个子条目，纯标题条目不落库（ADR-014）
- **分类**：`recategorize` 按内容加权打分给出建议，默认 dry-run 并打印「命中词与权重」，`--apply` 才写

**备份（P5-04）**

- `memoryhub backup`：把库提交到它自己的 git 仓库（离线、可回滚），并自动补 `.gitignore`
- `memoryhub backup --remote backup`：推送到远端；**只要库里有 sensitive/secret 条目就拒绝**，
  列出 id 并给出三条出路（audit 分类 / 降级 / `--allow-sensitive` 明确承担代价）
- 本地提交永远执行：拒绝提交只会丢历史，不会更安全

**敏感分级（P5-03）**

- 凭证形状（`sk-` / `ghp_` / `AKIA` / 私钥块 / `Bearer` / `key: xxx`）→ `secret` → **blocked**
- 强 PII（身份证 / 手机号 / 银行卡 / 完整住址）→ `sensitive` → **blocked**
- 弱 PII（城市、区县、学校）→ `personal` → 只标记，不阻断（把每个城市名都拦会让规则被无视）
- `memoryhub audit [--apply]` 用同一分类器复查已有条目；`sensitivity` 命令可人工升降

**冲突与取代（P3-05）**

- `memoryhub conflicts --ai`：先把每条记忆切成「片段」，把可能相关的片段**两两配对**，再**一次调用只判一对**
  ——无关条目物理上不在请求里，所以不存在「上下文一多就漏报」
- 配对有两条通道：词面相似度（IDF Dice）和**主题锚点**（开头三字相同）。后者不是锦上添花：真实那对
  只共享「毕业/论文」这类最常见词，按相似度排 4,803 对的第 207 名，靠锚点才进得来（升到第 18 名）
- 提案落 `inbox/conflicts/*.json`，**不写库**。`conflicts` 看列表，`conflicts --show <id>` 看两段正文、冲突片段与逐字证据
- 每条判定必须带两侧**逐字引用**，且必须能定位在**对应片段**里（不是整条条目里）；定位不到就降级为「矛盾」且不判胜负
- 判「取代」还要额外给出方向证据，且必须来自被判为较新的那一段。给不出就是「矛盾、无方向」——
  说「不知道谁对」比猜错方向有用
- `memoryhub supersede <旧id> --by <新id>` 是唯一会写取代链的命令：旧条目 `valid_until` 封窗、`status=archived`，新条目记 `supersedes`
- 四道闸：两个 id 必须存在、取代方必须已批准且未过期（否则旧条目归档后事实就从库里消失）、已被取代的不能重复取代、不许成环
- 旧条目正文超过 200 字时额外警告「这条可能不止一件事」—— 归档一条混装记忆会连带藏掉它剩下的内容，先拆分更稳
- 提案带 `scope`：`partial` 表示只有部分片段冲突，CLI 会明说「别整条归档」——
  这是第一版把 400 字汇总条目判成「整条重复」的直接补救
- 列出时**必须**打印「送审了多少对 / 有多少对没送审」：漏报是结构性的，只能让它可见，不能假装没有

> **2026-11-25 判定层已重做**：比较单位从「条目」降到「片段」，一次调用只判一对（无关条目物理上不在
> 请求里，所以不可能被稀释），并新增「主题锚点」通道召回只共享高频词的那类冲突。真实库实测 120 次调用
> 产出 24 条提案（20 重复 / 4 矛盾），全部带可定位的逐字证据。**但 4,803 个片段对只送审 120 个（2.5%）**，
> 命令会把每个数字都打出来 ——「没有冲突」只对送审的那批成立。见 ADR-018。

**回写分发（P5-06）**

- `memoryhub sync` 把当前有效记忆投影成一块「受管区块」，写进各 AI 自己的记忆文件
- 只动 `<!-- MEMORYHUB:BEGIN -->` 与 `<!-- MEMORYHUB:END -->` 之间；**区块外一个字节不碰**，删掉区块即完全还原
- 目标：`~/.claude/CLAUDE.md`、`~/.workbuddy/user-*/MEMORY.md`、`~/.codex/AGENTS.md`（缺失则新建）；
  路径与采集适配器共用 `CLAUDE_HOME` / `CODEX_HOME` / `WORKBUDDY_HOME`，**读哪写哪**
- 默认只出计划，`--write` 才落盘：写前备份 `*.bak-memoryhub-<时间戳>`，写后回读校验区块是否完整
- 导入时**先剥掉区块**再解析，否则同步会变成下一次导入的输入，库会慢慢填满自己的副本
- 只投影 `approved` 且未过期、未被取代的条目；`pending` 是提案，`superseded` 是历史，都不该分发给读者

**MCP（5 个工具，stdio）**

- `memory_search` / `memory_get` / `memory_list`：只返回**已批准且未过期**的条目，每条带来源与有效期
- `memory_propose`：AI 客户端可以交提案，但**只能进待审队列**；命中密钥形状直接 `blocked`
- `memory_status`：队列深度与索引健康（仅 `admin` 级别）
- 权限分级：`--permission read|propose|admin`（默认 `propose`）。权限按**进程**划分，
  不按客户端身份 —— MCP 的 `clientInfo` 是自报且无认证的，按它分级只是安全剧场
- 没有 `memory_write` —— **任何级别**都无法直接把内容写进正式库

**本机页面（P6-03）**

- `memoryhub serve` 起一个本地页面，把**两个队列**摆出来：待审条目、冲突提案；外加回写计划的总览
- 只绑 `127.0.0.1`，没有 `--host`；校验 `Host` 头（防 DNS rebinding）；每次启动一枚随机 token，走 header 不走 cookie
- 标准库 `http.server` + 单文件 HTML，**不加依赖、没有构建步骤**，页面里没有任何外部链接
- 写操作调的是 CLI 同一批函数（`set_status` / `apply_supersede`），所以 `blocked` 硬拒绝与取代链四道闸原样生效
- `sync --write` 不在页面里：它改的是别的工具每次会话都要加载的文件
- 长条目在「保留这条」旁单独告警 —— 401 字的混装条目被取代，会连带藏掉其它三件事

**其他**

- CLI：`init` `doctor` `new` `scan` `watch` `index` `audit` `diff` `log` `mcp` `sync` `serve` `list` `show` `search` `approve` `reject` `review` `conflicts` `supersede` `sensitivity` `backup` `stats`
- **索引**：SQLite FTS5 + trigram，中文可检索；删掉 `database/index.db` 会自动重建，结果不变
- **审核流**：`diff` 自动配对同来源条目并显示差异；`approve --all` 批量批准，`blocked` 永不被批量绕过
- **变更回看**：`log --since 7d` 直接读 vault 的 git 历史，列出新增/修改/删除的条目
- 保存前校验快照是否过期（`StaleEntryError`），不会把已批准的条目误降级
- 373 个单元测试（含 16 个片段配对、51 个冲突/取代、20 个回写分发、28 个本机页面、32 个 CLI 端到端、11 个 watcher、9 个索引、19 个 MCP 含真实 stdio 会话），全部通过

> **依赖**：`pyyaml` 与 `mcp`。阶段 1 曾零依赖；P2-01（真 YAML）与 P5-01（MCP）各引入一个，代价与复审时点见 ADR-005、ADR-012。

## 快速开始

依赖装在独立虚拟环境里。**不要用裸 `python` 跑本项目的 MCP 服务**——系统解释器通常没有
`pyyaml` / `mcp`，症状是客户端显示连不上，而不是报缺包。

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install "mcp>=2.0,<3" "pyyaml>=6.0"
# 中国大陆网络下清华源常返回空索引，改用官方源 + 代理：
# .venv/Scripts/python.exe -m pip install "mcp>=2.0,<3" "pyyaml>=6.0" --proxy http://127.0.0.1:7897

# 1. 创建记忆库（独立于本仓库，默认 D:/my-memory）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub init --vault D:/my-memory

# 2. 环境自检（exit 0 才算通过；WARN 不算失败）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub doctor

# 3. 采集：从已装的 AI 工具读入待审条目
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub import --dry-run   # 先看会导入什么
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub import             # 加 --ai 交给自己的模型整理

# 4. 审核（一屏一条：a 批 / r 拒 / c 改分类 / s 跳过 / q 退出）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub review

# 5. 冲突：让模型比对已批准条目（--ai 会把正文发到你自己配的端点）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub conflicts        # 看已有提案
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub conflicts --ai   # 生成提案，不写库
# 人拍板之后才落取代链：旧条目的 valid_until 封窗、status 转 archived
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub supersede <旧id> --by <新id>

# 6. 检索
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub search "论文"

# 7. 分类建议（先看，再决定是否写）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub recategorize          # dry run
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub recategorize --apply

# 8. 回写给各 AI：先看计划，确认后加 --write
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub sync --list
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub sync --diff
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub sync --write

# 9. 让别的 AI 客户端来查（stdio，前台阻塞）
PYTHONPATH=src .venv/Scripts/python.exe -m memoryhub mcp

# 10. 跑测试
PYTHONPATH=src .venv/Scripts/python.exe -m unittest discover -s tests -v
```

接到 AI 客户端（Claude Code 已实测通过，Claude Desktop / Cursor 同理）：

```json
{
  "mcpServers": {
    "memoryhub": {
      "type": "stdio",
      "command": "D:/project/MemoryHub/.venv/Scripts/python.exe",
      "args": ["-m", "memoryhub", "mcp", "--vault", "D:/my-memory"],
      "env": { "PYTHONPATH": "D:/project/MemoryHub/src" }
    }
  }
}
```

Claude Code 写在 `~/.claude.json` 的 `mcpServers` 下；用 `claude mcp list` 验证，
应看到 `√ Connected`。

**第二道门：工具权限。** 服务连通不代表 AI 能调用——Claude Code 默认拒绝 MCP 工具，
表现为 `permission not granted`。首次使用需要放行：

```bash
claude -p "我的毕业论文题目是什么" \
  --allowedTools "mcp__memoryhub__memory_search,mcp__memoryhub__memory_list,mcp__memoryhub__memory_get"
```

或者在客户端里用 `/permissions` 永久放行 `mcp__memoryhub__*`。这是客户端的权限模型，
不是服务端的问题——服务端只负责按 `--permission` 决定暴露哪些工具。

> 实机验证记录（2026-11-25）：Claude Code 通过 stdio 连上后，问「毕业论文题目 / 学校」，
> 模型调用 `memory_search` 两次（`毕业论文` 返回 4 条、`学校` 返回 1 条），答出了正确结果，
> 并且指出库里存在一条「题目尚未冻结」的旧条目与已定题目矛盾——它没有编造，也没有
> 擅自判断谁覆盖谁。**这正是 P3-05 冲突检测要解决的问题。**

安装为命令（可选，需要 hatchling）：

```bash
.venv/Scripts/python.exe -m pip install -e .
memoryhub doctor
```

装不上 `hatchling`（部分镜像源不提供）不影响使用：上面的 `PYTHONPATH=src` 方式功能完全一致，
只是每次要多打几个字。

## 目录结构

```
D:/project/MemoryHub        代码（本仓库，可开源）
  src/memoryhub/            实现
    adapters/               外部来源适配器（只读）
    watcher/                轮询 + 防抖 + 哈希
    mcp/                    只读 MCP 服务端
  tests/                    单元测试
  docs/                     定位、决策、路线、上游格式基线、策划书
  tools/                    一次性转换脚本
D:/my-memory                你的记忆（独立私有仓，绝不进本仓库）
  vault/                    正式记忆，按分类分目录
  inbox/pending/            待审提案
  inbox/rejected/           拒绝记录（保留，避免反复导入）
  inbox/conflicts/          冲突提案（JSON；不进检索、不算条目）
  sources/                  原始来源快照
  exports/                  对外导出的只读快照（**当前无任何代码写入**，见 ADR-002）
  logs/                     运行日志
```

## 两个仓库，三层边界

云端只有这一个仓，**只装代码**。真实使用时，每个用户的记忆在自己的仓里，远端由用户自己选（见 ADR-013）。

| 层 | 装什么 | 谁能看 | 由什么守住 |
|---|---|---|---|
| 代码仓（本仓） | 实现、测试、文档 | 公开 | pre-commit / commit-msg 扫描暂存内容**与提交信息** |
| 记忆仓工作副本 | 你的记忆 | 只有你 | `init` 拒绝把 vault 建在任何 git 工作树内 |
| 记忆仓远端 | 你的备份 | 你（建议私有 + 加密） | `init` 写入的 `pre-push` 钩子拒绝推到代码仓 |

```bash
sh tools/setup_dev.sh          # clone 后跑一次，启用钩子（core.hooksPath=tools/hooks）
python tools/lint_staged.py --all   # 手动全量扫描
```

一个容易低估的事实：**提交信息比文件更难撤回**。`force-push` 只让旧提交变成不可达，GitHub 仍保留对象，旧 SHA 可直接访问。所以信息扫描是独立的一道门，不是内容扫描的附属品。

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | 记忆模型、存储层、原子写入、CLI、测试 | 完成 |
| 1.5 | 加固：id 碰撞、读不出即报错、门禁分级、过期过滤 | 完成（ADR-009） |
| 2 | 采集适配器、文件监控、哈希去重、SQLite FTS5 索引 | **完成** |
| 3 | AI 整理（仅产出提案）、冲突检测、Pydantic 校验 | 未开始 |
| 4 | 审核流 + 差异预览（CLI） | **完成** |
| 5 | MCP stdio、权限分级、敏感检测、Git 自动备份 | **完成** |
| 6 | 打包（PyInstaller）、开机常驻、基础 Web UI | 未开始 |

## 明确不做

- 不向任何 AI 工具的原生记忆文件**回写**内容。只读采集 + 单向只读快照（ADR-002）。
- 不提供 `memory_write`：AI 只能提提案，不能直接入库（ADR-003、ADR-012）。
- 不训练、不托管模型。AI API 完全可选，不配置也能完成采集、审核、检索、备份。
- 不在 v0.x 建立云端账户、SaaS 或自建同步服务器。
- 不把 GitHub 当实时同步总线。

## 许可证

**尚未选定。** 在选定之前，本仓库默认保留所有权利（all rights reserved）。
倾向 Apache-2.0（含专利授权，便于被其他工具集成），但需要所有者拍板后再对外表述。

## 文档索引

| 文件 | 内容 |
|---|---|
| [`AGENTS.md`](AGENTS.md) | 工程契约：7 条不可推翻的决策、编码约定、明确禁止。**AI Agent 必读** |
| [`HANDOFF.md`](HANDOFF.md) | 现状交接：做到哪、怎么跑、已知问题、下一步 |
| [`docs/00-positioning.md`](docs/00-positioning.md) | 定位与竞品差异 |
| [`docs/01-decisions.md`](docs/01-decisions.md) | ADR 记录：12 条（ADR-010 待拍板），含每条的代价与复审时点 |
| [`docs/02-roadmap.md`](docs/02-roadmap.md) | 阶段拆解与可领工单 |
| [`docs/03-plan-review.md`](docs/03-plan-review.md) | 策划书 ↔ 当前决定的对照表（防止做回已否决功能） |
| [`docs/04-upstream-formats.md`](docs/04-upstream-formats.md) | 上游记忆来源的路径/格式基线，逐条标置信度（实盘/官方/镜像） |
| [`docs/reference/`](docs/reference/) | 项目策划书原文与可读版，只读 |

用 Claude Code / Cursor 接手时，`CLAUDE.md` 与 `.cursor/rules/project.mdc` 会指向上面的入口。

> **上游格式是猜的吗？** 不是。`docs/04-upstream-formats.md` 里每条路径都标了来源：
> `实盘`（在本机看到文件）、`官方`（厂商文档）、`镜像`（官方页抓不到时的副本）。
> 未验证的项目单独列了一节，并注明"不得据此写代码"。
