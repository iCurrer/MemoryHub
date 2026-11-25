"""The managed block: exactly one region of a foreign file belongs to us.

Why this module is dependency-free and sits below both ``adapters`` and
``sync``: the same markers must be understood by the writer and by every
reader, and a circular import between them would be the first thing to break.

The contract is narrow on purpose:

- Outside the markers, a file belongs to its owner (Claude Code, Codex,
  WorkBuddy, a human). Not one byte there is touched.
- Inside the markers, the block is a **projection** of the vault: derivable,
  regenerable, and never the source of truth.
- A reader must drop the block before treating text as memories. Without that,
  every ``sync`` would feed the next ``import``, and the vault would slowly
  refill with copies of itself -- an echo chamber that looks like growth.

ADR-002 still stands: adapters do not write. This is a separate layer that
appends a marked region; it never rewrites someone else's memory in their own
format, and deleting the block restores the file exactly.
"""

from __future__ import annotations

import re
from typing import Optional

#: Deliberately plain HTML comments: valid markdown, invisible when rendered,
#: and impossible to confuse with a heading someone wrote by hand.
BEGIN = "<!-- MEMORYHUB:BEGIN"
END = "<!-- MEMORYHUB:END -->"

_BLOCK = re.compile(
    re.escape(BEGIN) + r".*?" + re.escape(END),
    re.DOTALL,
)


def find_block(text: str) -> Optional[str]:
    """Return the managed block including its markers, or None."""
    match = _BLOCK.search(text)
    return match.group(0) if match else None


def strip_block(text: str) -> str:
    """Remove the managed block, leaving the owner's text untouched.

    Collapsing the blank lines the block left behind is done conservatively:
    at most one extra blank line is removed, because owners' files use blank
    lines as structure and a greedy cleanup would reformat their document.
    """
    if BEGIN not in text:
        return text
    stripped = _BLOCK.sub("", text)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.rstrip("\n") + "\n" if stripped.strip() else stripped


def replace_block(text: str, block: str) -> str:
    """Put ``block`` where the old one was, or append it.

    Appending uses two newlines so the block starts its own section; it never
    tries to "tidy" the preceding content into a matching style.
    """
    if find_block(text) is not None:
        return _BLOCK.sub(lambda _match: block, text, count=1)
    if not text.strip():
        return block + "\n"
    return text.rstrip("\n") + "\n\n" + block + "\n"


def has_block(text: str) -> bool:
    return find_block(text) is not None
