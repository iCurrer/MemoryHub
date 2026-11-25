"""Turn one source document into reviewable memories, using the user's model.

The prompt is the specification. Every line in it exists because leaving it out
produced a specific failure in an earlier version:

- "Do not rephrase" — without it the model rewrites a decision into a summary,
  and the user can no longer tell their own wording from the model's.
- "One fact per entry" — without it a 600-character paragraph of four unrelated
  facts comes back as one entry, which is exactly the ``mixed`` failure that
  measured 13.2% of the vault (``docs/04-classify-audit.md``).
- "quote must be copied from the source" — this is the only check that can be
  automated. If the quote cannot be found in the source, the entry is invented.

Tracing is not decoration. ``OrganizeResult.trace`` records, for every accepted
entry, whether its quote was located in the source text. Review prints that flag
so a human can spend attention on the unverified ones.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..models import MemoryEntry
from .provider import AIError, Provider
from .schema import ProposedEntry, extract_json, parse_proposals

#: Roughly 24k characters per call. Not a token count -- it is a guard against
#: sending a 200 kB document in one request where the provider would reject it
#: with an opaque context-length error. Chinese text is near 1 char per token,
#: so this stays well inside every current model's window with room for output.
BATCH_CHARS = 24000

#: A single call returning more than this means the model ignored "one fact per
#: entry"; keeping the cap makes the review queue tractable either way.
MAX_ENTRIES_PER_CALL = 200

CATEGORY_HELP = """\
- profile：关于用户的客观事实（身份、学历、所在地、设备、经历）
- preference：偏好、习惯、审美、倾向、不喜欢什么
- project：正在做或做过的项目、进度、计划、待办
- knowledge：可复用的知识、方法、结论、原理
- decision：规则、铁律、红线、对外口径、硬约束（含"必须/绝不/只能"这类指令）"""

SYSTEM_PROMPT = f"""\
你是一个记忆整理器。用户会给你一份与 AI 协作时留下的记忆文件，你要把它整理成一条条独立的、可人工审核的记忆。

必须遵守：
1. 一条记忆只表达一件事。一个段落里如果有多个主题，必须拆成多条。
2. 保持原文措辞。不要润色、不要改写成总结、不要把具体内容抽象成概念。
3. 原文没有的内容绝对不要出现。宁可少输出，也不要推测或补全。
4. 每条必须给出 quote：从原文逐字复制的片段（8-40 字），用于定位这条记忆来自哪里。quote 里不要改动任何字和标点。
5. 纯格式内容（空标题、分隔线、无关的元信息）跳过，不要为它们生成记忆。
6. 分类只能是以下五种之一：
{CATEGORY_HELP}

输出：只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。

{{"entries": [{{"category": "decision", "body": "整理后的记忆正文", "quote": "原文中的片段", "confidence": "high"}}]}}

confidence 取 high / medium / low：原文表述明确为 high，需要推断才成立为 low。"""

USER_TEMPLATE = """\
来源：{label}

<source>
{text}
</source>

把上面的内容整理成记忆，输出 JSON。"""


@dataclass
class OrganizeResult:
    """What one document produced, and what was thrown away."""

    entries: List[MemoryEntry] = field(default_factory=list)
    #: Item-level rejections from the schema validator and the quote check.
    problems: List[str] = field(default_factory=list)
    #: ``entry_id -> quote was located in the source``.
    trace: dict = field(default_factory=dict)
    #: How many calls were made (a long document is batched).
    calls: int = 0
    #: Characters actually sent to the provider.
    chars_sent: int = 0

    @property
    def unverified(self) -> int:
        return sum(1 for ok in self.trace.values() if not ok)


def _fingerprint(text: str) -> str:
    """Strip everything that a model may legitimately alter when copying.

    Whitespace, punctuation, quote style and full/half width are the four things
    that differ between a source line and a faithful quote of it. What remains
    -- CJK characters, letters, digits -- is the part that must match exactly.
    Removing more than that would let a paraphrase pass; removing less produces
    false rejections on punctuation alone.
    """
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.lower())


def quote_is_located(quote: str, source_fingerprint: str) -> bool:
    needle = _fingerprint(quote)
    if not needle:
        return False
    # A 3-character fingerprint matches almost anything; require some substance
    # before claiming the quote was found.
    if len(needle) < 4:
        return False
    return needle in source_fingerprint


def batch_split(text: str, limit: int = BATCH_CHARS) -> List[str]:
    """Split on blank lines so each call gets whole paragraphs.

    A hard character cut would slice a sentence in half and hand the model a
    fragment that it would then "complete" from imagination -- the failure mode
    this whole module is built to avoid.
    """
    if len(text) <= limit:
        return [text]
    batches: List[str] = []
    current: List[str] = []
    size = 0
    for block in re.split(r"(\n\s*\n)", text):
        if size + len(block) > limit and current:
            batches.append("".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        batches.append("".join(current))
    return batches


def _to_entry(
    proposal: ProposedEntry,
    *,
    source,
    source_hash: str,
) -> MemoryEntry:
    tags = ["ai-organized"]
    if source is not None and getattr(source, "layer", ""):
        tags.append(str(source.layer))
    return MemoryEntry.create(
        category=proposal.category,
        body=proposal.body,
        tags=tags,
        source_adapter=getattr(source, "adapter", "ai"),
        source_path=_as_posix(getattr(source, "path", "")),
        source_kind=getattr(source, "source_kind", "doc"),
        source_hash=source_hash,
        confidence=proposal.confidence,
        precedence=int(getattr(source, "precedence", 0) or 0),
        layer=str(getattr(source, "layer", "") or ""),
    )


def _as_posix(value) -> str:
    if not value:
        return ""
    try:
        return str(value).replace("\\", "/")
    except Exception:
        return ""


def organize(
    text: str,
    provider: Provider,
    *,
    source=None,
    max_entries: int = MAX_ENTRIES_PER_CALL,
) -> OrganizeResult:
    """Run one document through the model and return validated proposals."""
    result = OrganizeResult()
    if not text.strip():
        result.problems.append("源文件为空")
        return result

    label = getattr(source, "path", "inline text")
    label = str(label)
    source_fingerprint = _fingerprint(text)
    source_hash = ""
    if source is not None and hasattr(source, "content_hash"):
        try:
            source_hash = source.content_hash()
        except OSError:
            source_hash = ""

    batches = batch_split(text)
    for index, batch in enumerate(batches, start=1):
        suffix = f"（第 {index}/{len(batches)} 段）" if len(batches) > 1 else ""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(label=label + suffix, text=batch),
            },
        ]
        raw = provider.chat(messages)
        result.calls += 1
        result.chars_sent += len(batch)

        try:
            payload = extract_json(raw)
        except ValueError as exc:
            result.problems.append(f"第 {index} 段：{exc}")
            continue

        try:
            proposals, problems = parse_proposals(payload)
        except ValueError as exc:
            result.problems.append(f"第 {index} 段：{exc}")
            continue
        result.problems.extend(f"第 {index} 段 {problem}" for problem in problems)

        for proposal in proposals:
            if len(result.entries) >= max_entries:
                result.problems.append(
                    f"达到单文档上限 {max_entries} 条，其余已丢弃（说明源文件该人工拆分）"
                )
                break
            entry = _to_entry(proposal, source=source, source_hash=source_hash)
            located = quote_is_located(proposal.quote, source_fingerprint)
            result.trace[entry.id] = located
            if not located:
                result.problems.append(
                    f"{entry.id}: quote 在原文中定位不到，已收下但标记为待核实"
                )
            result.entries.append(entry)

    return result
