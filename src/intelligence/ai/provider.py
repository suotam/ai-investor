"""AIProvider abstraction + local OpenAI-compatible implementation (llama.cpp server).

The transport is injectable (`post_fn`) so all tests run offline. JSON output is requested
via instruction + extracted defensively; invalid JSON raises AIUnavailable-adjacent errors
that callers treat as "no analysis", never as data.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

from src.config import Settings
from src.logging_setup import get_logger

log = get_logger("intelligence.ai.provider")


class AIUnavailable(RuntimeError):
    """AI server not reachable / disabled. Callers degrade gracefully."""


class AIInvalidOutput(RuntimeError):
    """Model returned something that does not validate. Never stored as a proposal."""


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    finish_reason: str | None = None
    reasoning_chars: int = 0  # length of reasoning_content, if the model emitted one
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class AIProvider(ABC):
    name: str = "abstract"
    model: str = "unknown"

    @abstractmethod
    def complete(self, system: str, user: str, max_tokens: int = 2048) -> AIResponse:  # pragma: no cover
        raise NotImplementedError

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> dict:
        """Ask for JSON, parse defensively. Raises AIInvalidOutput on garbage. The final
        answer is read ONLY from message.content; reasoning_content never substitutes it."""
        resp = self.complete(system + "\nRespond with a single valid JSON object and nothing else.", user, max_tokens)
        if not resp.text.strip():
            raise AIInvalidOutput(
                "model produced no final content "
                f"(finish_reason={resp.finish_reason}, reasoning_chars={resp.reasoning_chars}, "
                f"completion_tokens={resp.completion_tokens}/{max_tokens})"
                + (" - completion budget likely exhausted by reasoning before the final JSON"
                   if resp.finish_reason == "length" and resp.reasoning_chars else "")
            )
        return extract_json(resp.text)


def extract_json(text: str) -> dict:
    """Extract the first JSON object from model output (handles ```json fences and prose)."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1)
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise AIInvalidOutput(f"no JSON object in model output: {text[:200]!r}")
        candidate = candidate[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIInvalidOutput(f"invalid JSON from model: {exc}") from exc
    if not isinstance(obj, dict):
        raise AIInvalidOutput("model output is not a JSON object")
    return obj


PostFn = Callable[[str, dict, float], dict]


class OpenAICompatProvider(AIProvider):
    """Local llama.cpp / any OpenAI-compatible /chat/completions endpoint. No cloud default."""

    def __init__(self, settings: Settings, post_fn: PostFn | None = None):
        self.name = settings.ai_provider
        self.model = settings.ai_model
        self.base_url = settings.ai_base_url.rstrip("/")
        self.timeout = settings.ai_timeout_seconds
        self.temperature = settings.ai_temperature
        self._post = post_fn or self._http_post

    def _http_post(self, url: str, payload: dict, timeout: float) -> dict:
        import requests

        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise AIUnavailable(f"AI server unreachable at {url}: {type(exc).__name__}") from exc

    def complete(self, system: str, user: str, max_tokens: int = 2048) -> AIResponse:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        log.debug(
            "AI request: endpoint=%s model=%s messages=%d prompt_chars=%d (~%d tokens) "
            "max_tokens=%d temperature=%s timeout=%.0fs",
            f"{self.base_url}/chat/completions", self.model, len(payload["messages"]),
            len(system) + len(user), (len(system) + len(user)) // 4, max_tokens,
            self.temperature, self.timeout,
        )
        data = self._post(f"{self.base_url}/chat/completions", payload, self.timeout)
        try:
            choice = data["choices"][0]
            message = choice["message"]
            text = message.get("content")
        except (KeyError, IndexError, TypeError) as exc:
            raise AIInvalidOutput(f"unexpected completion payload keys: {sorted(data) if isinstance(data, dict) else type(data)}") from exc
        usage = data.get("usage") or {}
        resp = AIResponse(
            text=text or "",
            provider=self.name,
            model=data.get("model") or self.model,
            finish_reason=choice.get("finish_reason"),
            # reasoning models (e.g. Muse Glimmer via llama.cpp) return reasoning separately;
            # it is NEVER parsed as the structured result - recorded for diagnostics only
            reasoning_chars=len(message.get("reasoning_content") or ""),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        log.debug(
            "AI response: finish_reason=%s content_chars=%d reasoning_chars=%d "
            "prompt_tokens=%s completion_tokens=%s keys=%s",
            resp.finish_reason, len(resp.text), resp.reasoning_chars,
            resp.prompt_tokens, resp.completion_tokens, sorted(message.keys()),
        )
        return resp

    def health(self) -> dict:
        try:
            data = self._post(f"{self.base_url}/chat/completions",
                              {"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                               "max_tokens": 1}, min(self.timeout, 10))
            return {"available": True, "provider": self.name, "model": data.get("model") or self.model,
                    "base_url": self.base_url}
        except (AIUnavailable, AIInvalidOutput) as exc:
            return {"available": False, "provider": self.name, "model": self.model,
                    "base_url": self.base_url, "error": str(exc)}


def get_ai_provider(settings: Settings, post_fn: PostFn | None = None) -> AIProvider:
    if not settings.ai_enabled:
        raise AIUnavailable("AI is disabled (ai.enabled=false in config/settings.yaml)")
    return OpenAICompatProvider(settings, post_fn=post_fn)
