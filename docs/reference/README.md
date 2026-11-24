# 参考资料

本目录存放**项目外部来源的原始材料**，只读，不参与构建。

| 文件 | 说明 |
|---|---|
| `plan-v0.1.docx` | 项目策划书 V0.1 原文（2026-11-23）。**权威版本**，含完整表格样式 |
| `plan-v0.1.md` | 由 docx 自动转换的可读版本，供 AI Agent 直接读取 |

## 使用规则

1. **策划书是输入，不是需求。** 它描述的是"当时认为该怎么做"，其中若干方案已被 [`../01-decisions.md`](../01-decisions.md) 推翻。判断依据永远是 ADR，不是本文。
2. 二者冲突时，以 ADR 为准。冲突清单见 [`../03-plan-review.md`](../03-plan-review.md)。
3. 不要修改本目录下的文件。需要更新策划书时，新增 `plan-v0.2.docx` 并重新转换。
4. `plan-v0.1.md` 由脚本从 docx 转换，正文未改写；发现转换错误请修转换脚本，不要手改 md。

## 重新生成 markdown

```bash
# 1. 用本地 editor_sdk（或 Word「另存为网页」）把 docx 渲染成 HTML
# 2. 转成 markdown（在仓库根目录执行）
python tools/html2md.py plan.html docs/reference/plan-v0.1.md
```

转换器只处理 `h1`-`h6` / `p` / `table` / `br`，并把含制表符框线的段落自动包成 ` ```text ` 代码块，
以保留架构图的等宽对齐。它**不改写任何文字**，只做版式映射。
