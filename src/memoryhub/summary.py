"""One scannable sentence per memory -- derived, disposable, rebuildable.

Why this exists at all, given ADR-016 says the model may copy but never compose:
that rule protects the **fact source**. A card title is not a fact source. The
entry body stays byte-identical to the original excerpt, keeps its ``quote``, and
remains the only thing search and write-back ever read. The sentence produced
here lives in a sidecar file, is marked ``derived`` in the page, and can be
deleted whole without losing anything a later reader would miss.

That is the line this module must not cross:

- **Never written into the vault.** ``database/summaries.json`` is keyed by entry
  id; the entry files are untouched, so ``git diff`` on the vault stays a record
  of what the *user* decided.
- **Never enters retrieval or write-back.** Search, ``memory_list``, and the
  managed block read ``body``. A wrong summary costs a bad first impression; a
  wrong body would cost a wrong fact in three AIs' context.
- **Never trusted for its claim.** Every summary must carry a ``quote`` copied
  verbatim out of the body; if the quote cannot be located, the summary is
  discarded and the card falls back to the extracted first line. A model that
  invents a sentence usually also invents its evidence -- requiring both makes
  invention visible instead of fluent.
- **Never stale.** The sidecar records a hash of the body it summarized. Edit the
  body (or re-split it) and the stored sentence stops matching, so the page falls
  back to extraction until the next ``summarize`` run. Stale text that still
  *looks* authoritative is worse than no text.

What the library looks like without this module: cards titled "二、经历与求职背景"
-- section headings, because that is what the entry literally starts with. True
and useless. The complaint that produced this file was "为什么不显示 AI 整理过的".
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ai.provider import AIError, Provider
from .models import MemoryEntry

#: Where the derived sentences live. ``database/`` already holds the rebuildable
#: FTS index; this is the same kind of artefact -- deletable, regenerable, never
#: the source of truth.
FILE_NAME = "summaries.json"

#: Same ceiling as the page's extracted headline. Longer and the "scan one line
#: per card" property is gone, which is the only reason the field exists.
MAX_CHARS = 44

SYSTEM_PROMPT = """你在为用户的个人记忆库写卡片标题。每条记忆是一段从原始笔记里逐字摘出的文字。

规则：
1. 用一句陈述句说清这条记忆讲了什么，不超过 40 个汉字。主语是用户本人，或者这条记忆讨论的对象。
2. 只能使用原文里出现的信息。不要推断，不要补充背景，不要评价，不要写"用户喜欢/擅长"这类原文没有的判断。
3. 如果原文是一份清单或一个章节，就说清它涵盖什么（例："电脑与手机等设备清单"），不要假装它只有一条事实。
4. 如果原文是被截断的残句，就照实概括它的前半部分，不要补全。
5. quote 必须是原文中**逐字复制**的一段连续文字（10 到 40 字），用来支撑这句话。找不到可以支撑的原文，就把 line 写成空字符串。

只输出一行 JSON，不要代码块：
{"line": "...", "quote": "..."}"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def path_for(root: Path) -> Path:
    return Path(root) / "database" / FILE_NAME


def body_hash(text: str) -> str:
    """Short hash of the body a summary was written from.

    Short on purpose: this is a freshness check, not a security boundary, and a
    human reading the JSON should be able to see whether two entries share one.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def load(root: Path) -> Dict[str, Dict[str, Any]]:
    target = path_for(root)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}


def save(root: Path, data: Dict[str, Dict[str, Any]]) -> None:
    target = path_for(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: data[key] for key in sorted(data)}
    target.write_text(
        json.dumps(ordered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def for_entry(
    data: Dict[str, Dict[str, Any]], entry: MemoryEntry
) -> Optional[str]:
    """The stored sentence for ``entry``, or ``None`` when absent or stale."""
    slot = data.get(entry.id)
    if not slot:
        return None
    if slot.get("body_hash") != body_hash(entry.body):
        return None
    text = str(slot.get("line") or "").strip()
    return text or None


def pending(
    entries: Iterable[MemoryEntry], data: Dict[str, Dict[str, Any]]
) -> List[MemoryEntry]:
    """Entries with no usable sentence: never summarized, or edited since."""
    return [entry for entry in entries if for_entry(data, entry) is None]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _clean(line: str) -> str:
    text = re.sub(r"\s+", " ", str(line or "")).strip()
    text = text.strip("“”\"'` ")
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip(" ，。、；：,;:") + "…"
    return text


def parse_reply(raw: str, body: str) -> Optional[Dict[str, str]]:
    """Pick ``{"line", "quote"}`` out of a reply, and verify the quote exists.

    Returns ``None`` when the model refused (empty ``line``) or when its quote is
    not actually in the body. Both cases are normal outcomes, not errors: the
    page simply keeps showing the extracted first line.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", text).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    line = _clean(data.get("line"))
    quote = str(data.get("quote") or "").strip()
    if not line or not quote:
        return None
    haystack = _normalise(body)
    needle = _normalise(quote)
    if len(needle) < 6 or needle not in haystack:
        return None
    return {"line": line, "quote": quote}


def _messages(entry: MemoryEntry) -> List[Dict[str, str]]:
    body = entry.body.strip()
    if len(body) > 1600:
        body = body[:1600] + "…"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"分类：{entry.category}\n\n原文：\n{body}",
        },
    ]


def generate(
    entries: List[MemoryEntry],
    data: Dict[str, Dict[str, Any]],
    provider: Provider,
    on_progress=None,
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Summarize ``entries`` and merge the results into ``data``.

    Returns the merged mapping and the ids that produced nothing usable. Kept
    pure-ish (no disk writes) so the caller decides when to persist -- a run that
    dies halfway should not leave a half-written sidecar behind.
    """
    merged = dict(data)
    failures: List[str] = []
    for index, entry in enumerate(entries, start=1):
        if on_progress is not None:
            on_progress(index, len(entries), entry)
        try:
            raw = provider.chat(_messages(entry))
        except AIError:
            # Configuration, key or transport. Repeating it fifty times only
            # produces fifty identical failures and a sidecar full of nothing --
            # the caller needs the reason, not a tally.
            raise
        except Exception:  # noqa: BLE001 - one bad reply must not stop the run
            failures.append(entry.id)
            continue
        parsed = parse_reply(raw, entry.body)
        if parsed is None:
            failures.append(entry.id)
            continue
        merged[entry.id] = {
            "line": parsed["line"],
            "quote": parsed["quote"],
            "body_hash": body_hash(entry.body),
            "model": provider.cfg.model,
            "at": _now(),
        }
    return merged, failures


def stats(
    entries: Iterable[MemoryEntry], data: Dict[str, Dict[str, Any]]
) -> Dict[str, int]:
    total = 0
    covered = 0
    for entry in entries:
        total += 1
        if for_entry(data, entry) is not None:
            covered += 1
    return {"total": total, "covered": covered, "missing": total - covered}
