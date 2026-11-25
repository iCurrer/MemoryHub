"""OpenAI-compatible provider, stdlib only.

Two constraints shape this file:

- **ADR-005 keeps the runtime dependency list short.** The project already carries
  ``pyyaml`` and ``mcp``; adding an SDK for one HTTP POST is not worth a third.
  ``urllib.request`` is enough and works offline-of-package-managers.
- **The API key never touches the config file.** ``config.save`` writes plain
  text and the repo is public. The key is read from an environment variable whose
  *name* is configured. A config file that leaks a key is a config file that
  gets committed by accident.

Nothing here decides whether to call the model; that is ``organize.py``. This
file only knows how to reach an endpoint and turn its failures into messages a
human can act on.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .. import config as hub_config

#: Which environment variable holds the key, when the config does not say.
DEFAULT_KEY_ENV = "MEMORYHUB_API_KEY"

#: A single source file can produce a long prompt, and a slow model can take a
#: while. Generous, but bounded: an unbounded read is how a CLI appears hung.
DEFAULT_TIMEOUT = 120.0


class AIError(RuntimeError):
    """Raised when the model cannot be reached or returns something unusable.

    The message is written for the person at the terminal, not for a log: it
    names the setting to change or the command to run.
    """


def _flat(data: Dict[str, Any], key: str) -> Optional[Any]:
    """Read ``[ai] key`` or ``ai_key`` from config, nested first.

    ``config.save`` only writes flat scalars, so a user who edits the file by
    hand will naturally write a table. Accepting both means neither style is
    silently ignored -- a config that parses but does nothing is worse than one
    that fails.
    """
    table = data.get("ai")
    if isinstance(table, dict) and key in table:
        return table[key]
    return data.get(f"ai_{key}")


_SETUP_HINT = """\
AI 未配置。在 {path} 里加一段：

    [ai]
    base_url = "https://api.deepseek.com/v1"
    model = "deepseek-chat"

然后设置环境变量（密钥不写进配置文件）：
    PowerShell   $env:{key_env}="your-key-here"
    cmd          set {key_env}=your-key-here
    bash         export {key_env}=your-key-here
"""


@dataclass(frozen=True)
class AIConfig:
    base_url: str
    model: str
    api_key_env: str = DEFAULT_KEY_ENV
    timeout: float = DEFAULT_TIMEOUT
    temperature: float = 0.0
    max_tokens: int = 8192

    @classmethod
    def from_config(cls) -> Optional["AIConfig"]:
        """Return ``None`` when the user has not configured AI at all.

        ``None`` is a normal answer, not an error: every non-AI command must keep
        working on a machine with no key (P3-01 acceptance criterion).
        """
        data = hub_config.load()
        base_url = _flat(data, "base_url")
        model = _flat(data, "model")
        if not base_url and not model:
            return None
        if not base_url or not model:
            missing = "base_url" if not base_url else "model"
            raise AIError(
                f"[ai] 配置不完整，缺少 {missing}。\n"
                + _SETUP_HINT.format(
                    path=hub_config.config_path(),
                    key_env=str(_flat(data, "api_key_env") or DEFAULT_KEY_ENV),
                )
            )
        timeout = _flat(data, "timeout")
        temperature = _flat(data, "temperature")
        max_tokens = _flat(data, "max_tokens")
        return cls(
            base_url=str(base_url).rstrip("/"),
            model=str(model),
            api_key_env=str(_flat(data, "api_key_env") or DEFAULT_KEY_ENV),
            timeout=float(timeout) if timeout is not None else DEFAULT_TIMEOUT,
            temperature=float(temperature) if temperature is not None else 0.0,
            max_tokens=int(max_tokens) if max_tokens is not None else 8192,
        )

    def api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise AIError(
                f"环境变量 {self.api_key_env} 为空。\n"
                + _SETUP_HINT.format(
                    path=hub_config.config_path(), key_env=self.api_key_env
                )
            )
        return key


class Provider:
    """One endpoint, one model, one POST shape."""

    def __init__(self, cfg: AIConfig, *, transport=None) -> None:
        self.cfg = cfg
        #: Injectable for tests: takes (url, payload, headers, timeout) and
        #: returns the decoded JSON body. Defaults to a real HTTPS call.
        self._transport = transport or self._http_post

    # ---------------------------------------------------------------- request

    def _payload(self, messages: List[Dict[str, str]]) -> Dict[str, Any]:
        return {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key()}",
            # Some gateways reject requests without a UA.
            "User-Agent": "memoryhub/0.0.1",
        }

    def _http_post(
        self, url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:400]
            except Exception:
                pass
            hint = ""
            if exc.code in (401, 403):
                hint = f"\n检查 {self.cfg.api_key_env} 是否有效、是否有该模型的权限。"
            elif exc.code == 404:
                hint = f"\n检查 base_url 是否包含 /v1，以及 model={self.cfg.model!r} 是否存在。"
            elif exc.code == 429:
                hint = "\n触发限流，稍后重试或换更小的源文件。"
            raise AIError(f"HTTP {exc.code} from {url}{hint}\n{detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise AIError(
                f"无法连接 {url}：{exc.reason}\n"
                "如果是代理问题，取消 http_proxy/https_proxy，或确认代理可达。"
            ) from exc
        except TimeoutError as exc:
            raise AIError(f"请求超时（{timeout}s）：{url}\n源文件过大时可分批整理。") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AIError(f"响应不是 JSON（前 300 字）：{raw[:300]}") from exc

    # ------------------------------------------------------------------- call

    def chat(self, messages: List[Dict[str, str]]) -> str:
        """Send ``messages`` and return the assistant's text content."""
        url = f"{self.cfg.base_url}/chat/completions"
        data = self._transport(
            url, self._payload(messages), self._headers(), self.cfg.timeout
        )
        return self._extract_text(data)

    @staticmethod
    def _extract_text(data: Any) -> str:
        if not isinstance(data, dict):
            raise AIError(f"响应顶层不是对象，而是 {type(data).__name__}")
        if "error" in data:
            message = data["error"]
            if isinstance(message, dict):
                message = message.get("message", message)
            raise AIError(f"服务端返回错误：{message}")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AIError("响应里没有 choices，可能不是 OpenAI 兼容接口")
        first = choices[0]
        if not isinstance(first, dict):
            raise AIError("choices[0] 不是对象")
        message = first.get("message")
        if not isinstance(message, dict):
            raise AIError("choices[0].message 缺失")
        content = message.get("content")
        if isinstance(content, list):
            # Some providers return content parts instead of a plain string.
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        if not isinstance(content, str) or not content.strip():
            # A reasoning model spends the same ``max_tokens`` budget on its
            # chain of thought first, so a budget that is generous for prose can
            # leave nothing for the answer. Reporting that as "empty content"
            # sends the reader looking for a prompt bug that is not there.
            if first.get("finish_reason") == "length":
                raise AIError(
                    "模型在写出答案前就用完了 max_tokens（finish_reason=length）。\n"
                    "推理模型会先把配额花在思维链上：把配置里的 [ai] max_tokens 调大"
                    "（例如 32768），或换用非推理模型。"
                )
            raise AIError("模型返回了空内容")
        return content
