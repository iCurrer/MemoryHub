"""Configuration handling for MemoryHub.

Config lives in ``~/.memoryhub/config.toml`` (override with MEMORYHUB_HOME).
Only non-secret values belong here; API keys are referenced by name and stored
in the OS credential store (not implemented in phase 1).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

#: Default vault location. Deliberately independent from the project name so
#: that renaming the tool never invalidates user data paths.
DEFAULT_VAULT = "D:/my-memory"

#: Directory layout created inside a vault root.
LAYOUT = (
    "vault",
    "inbox/pending",
    "inbox/conflicts",
    "inbox/rejected",
    "database",
    "sources",
    "exports",
    "logs",
)


def home() -> Path:
    override = os.environ.get("MEMORYHUB_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".memoryhub"


def config_path() -> Path:
    return home() / "config.toml"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load() -> Dict[str, Any]:
    path = config_path()
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def save(data: Dict[str, Any]) -> Path:
    """Write config atomically. Flat scalar values only."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# MemoryHub configuration. Never store secrets in this file."]
    for key in sorted(data):
        value = data[key]
        if isinstance(value, bool):
            lines.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, (int, float)):
            lines.append(f"{key} = {value}")
        else:
            lines.append(f"{key} = {_quote(str(value))}")
    text = "\n".join(lines) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path


def resolve_vault(cli_override: Optional[str] = None) -> Path:
    """Resolution order: CLI flag -> env var -> config file -> default."""
    if cli_override:
        return Path(cli_override).expanduser().resolve()
    env = os.environ.get("MEMORYHUB_VAULT")
    if env:
        return Path(env).expanduser().resolve()
    data = load()
    stored = data.get("vault")
    if stored:
        return Path(str(stored)).expanduser().resolve()
    return Path(DEFAULT_VAULT).resolve()


def as_posix(path: Path) -> str:
    """Store paths with forward slashes to avoid TOML escaping problems."""
    return Path(path).resolve().as_posix()
