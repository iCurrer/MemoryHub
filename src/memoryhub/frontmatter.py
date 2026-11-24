"""YAML frontmatter reader/writer.

Phase 1 shipped a hand-rolled parser that understood only flat ``key: value``
pairs and inline lists. That was enough to prove the data model, but it made the
vault fragile in exactly the way the product cannot afford: an entry edited in
Obsidian (which writes block lists) became unreadable. Phase 2 replaces the
implementation with PyYAML while keeping the same ``split`` / ``join`` contract.

Deliberate deviations from plain ``yaml.safe_load``:

- YAML 1.1 treats ``on`` / ``off`` / ``yes`` / ``no`` as booleans. A tag named
  ``on`` would come back as ``True`` and fail validation, so the loader uses
  YAML 1.2 semantics: only ``true`` / ``false`` are booleans.
- YAML turns ISO timestamps into ``date`` objects and the schema stores them as
  strings, so every ``date`` / ``datetime`` is normalised back to ISO 8601.
- Duplicate keys are rejected instead of silently keeping the last one.

Phase 1 called this module's parser "temporary" (ADR-005). The migration is
recorded in ADR-005 as a partial supersession.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, Tuple

import yaml

DELIM = "---"

#: Match the ``---`` fence on its own line, tolerating surrounding whitespace.
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


class FrontmatterError(ValueError):
    """Raised when a frontmatter block cannot be parsed or serialised."""


class _Loader(yaml.SafeLoader):
    """SafeLoader with duplicate-key detection and YAML 1.2 bool rules."""

    def construct_mapping(self, node: Any, deep: bool = False) -> Dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise FrontmatterError(f"duplicate frontmatter key: {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


#: Rebuild the resolver table without the YAML 1.1 bool words, then add back the
#: ``true`` / ``false`` only resolver. Done on a subclass so ``SafeLoader`` itself
#: is untouched.
_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in mappings if tag != "tag:yaml.org,2002:bool"]
    for first, mappings in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_Loader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    _BOOL_RE,
    list("tTfF"),
)


def _normalise(value: Any) -> Any:
    """Coerce YAML-native scalars back into the shapes the schema expects."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    return value


def split(text: str) -> Tuple[Dict[str, Any], str]:
    """Split a document into (metadata, body).

    A leading UTF-8 BOM is stripped: Windows editors and the clipboard still
    produce one, and it would otherwise hide the whole frontmatter block.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith(DELIM):
        return {}, text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != DELIM:
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == DELIM:
            end = index
            break
    if end is None:
        raise FrontmatterError("frontmatter block is not terminated by '---'")

    block = "".join(lines[1:end])
    body = "".join(lines[end + 1 :])
    if not block.strip():
        return {}, body

    try:
        data = yaml.load(block, Loader=_Loader)
    except FrontmatterError:
        raise
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc

    if data is None:
        return {}, body
    if not isinstance(data, dict):
        raise FrontmatterError(
            f"frontmatter must be a mapping, got {type(data).__name__}"
        )
    return {str(key): _normalise(value) for key, value in data.items()}, body


def join(meta: Dict[str, Any], body: str) -> str:
    """Render metadata + body back into a frontmatter document."""
    if not isinstance(meta, dict):
        raise FrontmatterError(
            f"frontmatter must be a dict, got {type(meta).__name__}"
        )
    try:
        block = yaml.safe_dump(
            meta,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4096,
        )
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"cannot serialise frontmatter: {exc}") from exc
    if not block.strip():
        block = "{}\n"
    return f"{DELIM}\n{block}{DELIM}\n{body}"
