# CLAUDE.md

工程契约在 [`AGENTS.md`](AGENTS.md)，**请完整阅读后再动手**。本文件只做导航，不重复规则（避免两处规则漂移）。

## 读什么，什么顺序

1. [`AGENTS.md`](AGENTS.md) —— 不可推翻的 7 条决策、编码约定、明确禁止
2. [`HANDOFF.md`](HANDOFF.md) —— 当前完成到哪、怎么跑、已知问题、下一步
3. [`docs/02-roadmap.md`](docs/02-roadmap.md) —— 领一个工单，按编号顺序做
4. [`docs/01-decisions.md`](docs/01-decisions.md) —— 想推翻某条决策时，先看它的代价与复审时点

## 三条最容易踩的

- 不实现"把记忆写回其他 AI 工具"（ADR-002，策划书原方案已否决）
- AI 产出只能进 `inbox/`，`status=pending`，绝不直接落 `vault/`（ADR-003）
- `vault/` 不进本仓库（ADR-007）

## 提交前必须过

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m memoryhub doctor
```

两项都要绿。新增功能必须补测试。
