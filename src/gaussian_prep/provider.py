from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .prompts import role_prompt

T = TypeVar("T", bound=BaseModel)


class ProviderError(RuntimeError):
    pass


class IncompleteCompletion(ProviderError):
    """A response ended before producing a complete role result; safe to retry within budget."""


PROVIDERS = {
    "opencode": {"label": "OpenCode Go", "base_url": "https://opencode.ai/zen/go/v1", "prefix": "OPENCODE", "protocol": "chat/completions", "auto_protocol": True},
    "deepseek": {"label": "DeepSeek (direct)", "base_url": "https://api.deepseek.com", "prefix": "DEEPSEEK", "protocol": "chat/completions"},
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "prefix": "OPENAI", "protocol": "chat/completions"},
    "anthropic": {"label": "Anthropic (Claude)", "base_url": "https://api.anthropic.com/v1", "prefix": "ANTHROPIC", "protocol": "messages"},
    "google": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "prefix": "GOOGLE", "protocol": "chat/completions"},
    "xai": {"label": "xAI (Grok)", "base_url": "https://api.x.ai/v1", "prefix": "XAI", "protocol": "chat/completions"},
    "mistral": {"label": "Mistral AI", "base_url": "https://api.mistral.ai/v1", "prefix": "MISTRAL", "protocol": "chat/completions"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "prefix": "GROQ", "protocol": "chat/completions"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "prefix": "OPENROUTER", "protocol": "chat/completions"},
    "together": {"label": "Together AI", "base_url": "https://api.together.xyz/v1", "prefix": "TOGETHER", "protocol": "chat/completions"},
    "fireworks": {"label": "Fireworks AI", "base_url": "https://api.fireworks.ai/inference/v1", "prefix": "FIREWORKS", "protocol": "chat/completions"},
    "deepinfra": {"label": "DeepInfra", "base_url": "https://api.deepinfra.com/v1/openai", "prefix": "DEEPINFRA", "protocol": "chat/completions"},
    "perplexity": {"label": "Perplexity", "base_url": "https://api.perplexity.ai", "prefix": "PERPLEXITY", "protocol": "chat/completions"},
    "cohere": {"label": "Cohere", "base_url": "https://api.cohere.ai/compatibility/v1", "prefix": "COHERE", "protocol": "chat/completions"},
    "moonshot": {"label": "Moonshot (Kimi)", "base_url": "https://api.moonshot.ai/v1", "prefix": "MOONSHOT", "protocol": "chat/completions"},
    "zhipu": {"label": "Zhipu GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "prefix": "ZHIPU", "protocol": "chat/completions"},
    "qwen": {"label": "Qwen (DashScope)", "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "prefix": "QWEN", "protocol": "chat/completions"},
    "cerebras": {"label": "Cerebras", "base_url": "https://api.cerebras.ai/v1", "prefix": "CEREBRAS", "protocol": "chat/completions"},
    "sambanova": {"label": "SambaNova", "base_url": "https://api.sambanova.ai/v1", "prefix": "SAMBANOVA", "protocol": "chat/completions"},
    "nvidia": {"label": "NVIDIA NIM", "base_url": "https://integrate.api.nvidia.com/v1", "prefix": "NVIDIA", "protocol": "chat/completions"},
    "novita": {"label": "Novita AI", "base_url": "https://api.novita.ai/v3/openai", "prefix": "NOVITA", "protocol": "chat/completions"},
    "hyperbolic": {"label": "Hyperbolic", "base_url": "https://api.hyperbolic.xyz/v1", "prefix": "HYPERBOLIC", "protocol": "chat/completions"},
    "github": {"label": "GitHub Models", "base_url": "https://models.github.ai/inference", "prefix": "GITHUB", "protocol": "chat/completions"},
    "ollama": {"label": "Ollama (local)", "base_url": "http://localhost:11434/v1", "prefix": "OLLAMA", "protocol": "chat/completions", "keyless": True},
    "custom": {"label": "Custom OpenAI-compatible", "base_url": "", "prefix": "CUSTOM", "protocol": "chat/completions"},
}


def provider_base_url(provider: str) -> str:
    """Resolve a provider's endpoint, reading CUSTOM_BASE_URL for the custom entry."""
    if provider not in PROVIDERS:
        raise ProviderError("Choose a provider from the menu")
    if provider == "custom":
        url = os.getenv("CUSTOM_BASE_URL", "").strip()
        if not url:
            raise ProviderError("Set CUSTOM_BASE_URL to your OpenAI-compatible endpoint")
        return url.rstrip("/")
    return PROVIDERS[provider]["base_url"].rstrip("/")

# Local concurrency heuristics; account-specific quotas may be lower.
MODEL_LIMITS: tuple[tuple[str, int], ...] = (
    ("gpt", 8), ("o1", 8), ("o3", 8), ("o4", 8),
    ("claude", 16),
    ("gemini", 8),
    ("glm", 4),
    ("grok", 30),
    ("qwen", 16),
    ("kimi", 8), ("moonshot", 8),
    ("mistral", 8),
    ("llama", 8),
)
DEFAULT_CONCURRENCY = 4

# Fallback per provider when the model name does not match a known family above.
PROVIDER_LIMITS = {
    "opencode": 4, "deepseek": 500, "openai": 8, "anthropic": 16, "google": 8,
    "xai": 30, "mistral": 8, "groq": 30, "openrouter": 8, "together": 8,
    "fireworks": 8, "deepinfra": 8, "perplexity": 8, "cohere": 8, "moonshot": 8,
    "zhipu": 4, "qwen": 16, "cerebras": 30, "sambanova": 16, "nvidia": 8,
    "novita": 8, "hyperbolic": 8, "github": 4, "ollama": 4, "custom": 4,
}


def concurrency_limit(provider: str, model: str) -> int:
    """Local parallel-request ceiling; this is not a provider quota guarantee."""
    lowered = model.lower()
    if provider == "deepseek" or "deepseek" in lowered:
        if "pro" in lowered:
            return 500
        if "flash" in lowered or lowered.startswith(("deepseek-chat", "deepseek-reasoner")):
            return 2500
        return 500
    for substring, limit in MODEL_LIMITS:
        if substring in lowered:
            return limit
    return PROVIDER_LIMITS.get(provider, DEFAULT_CONCURRENCY)


class ModelProvider:
    """One model and one API key shared by all five roles. Safe to call from many threads."""
    def __init__(self, model: str, api_key: str, log_dir: Path, max_calls: int = 400, provider: str = "opencode", session_path: Path | None = None):
        if provider not in PROVIDERS:
            raise ProviderError("Choose a provider from the menu")
        self.name = provider
        self.spec = PROVIDERS[provider]
        self.base_url = provider_base_url(provider)
        if provider == "opencode" and model.startswith("opencode-go/"):
            model = model.removeprefix("opencode-go/")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
            raise ProviderError(f"Set {self.spec['prefix']}_MODEL or type a model ID at the prompt")
        if not api_key and not self.spec.get("keyless"):
            raise ProviderError(f"Set {self.spec['prefix']}_API_KEY or enter the API key at the prompt")
        if max_calls < 1:
            raise ProviderError("The call budget must be positive")
        self.model, self.api_key = model, api_key or "not-needed"
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.max_calls = max_calls
        self.calls, self._log_seq = 0, 0
        self._lock = threading.Lock()
        self.session_id = hashlib.sha256(str((session_path if session_path is not None else log_dir).resolve()).encode()).hexdigest()

    def request_parts(self, role: str, payload: dict, schema: type[T], history: list) -> tuple[str, dict, dict, str]:
        system = role_prompt(role, payload.get("prep_profile")) + "\nReturn a single JSON object matching this JSON schema:\n" + json.dumps(schema.model_json_schema())
        user = json.dumps({**payload, "schema_correction": history}, ensure_ascii=False)
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "gaussian-prep/0.1.0"}
        if self.name == "opencode":
            headers["x-opencode-session"] = self.session_id
        # OpenCode documents different wire formats for these model families.
        protocol = self.spec.get("protocol", "chat/completions")
        if self.spec.get("auto_protocol") and self.model.startswith("qwen"):
            protocol = "messages"
        elif self.spec.get("auto_protocol") and self.model.startswith(("gpt-", "grok-", "muse-spark-")):
            protocol = "responses"
        if protocol == "messages":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
            body = {"model": self.model, "system": system, "messages": [{"role": "user", "content": user}], "max_tokens": 32768, "stream": False}
        elif protocol == "responses":
            body = {"model": self.model, "instructions": system, "input": [{"role": "user", "content": user}], "max_output_tokens": 32768, "stream": False}
        else:
            body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "response_format": {"type": "json_object"}, "max_tokens": 32768, "stream": False}
        return f"{self.base_url}/{protocol}", headers, body, protocol

    def completion_text(self, data: dict, protocol: str) -> str:
        if protocol == "messages":
            if data.get("stop_reason") != "end_turn":
                raise IncompleteCompletion(f"Provider did not finish normally: {data.get('stop_reason', 'unknown')}")
            return "".join(part.get("text", "") for part in data.get("content", []) if part.get("type") == "text")
        if protocol == "responses":
            if data.get("status") != "completed":
                raise IncompleteCompletion(f"Provider did not finish normally: {data.get('status', 'unknown')}")
            return "".join(part.get("text", "") for item in data.get("output", []) if item.get("type") == "message" for part in item.get("content", []) if part.get("type") == "output_text")
        choices = data.get("choices", [])
        if not choices:
            raise ProviderError(f"{self.spec['label']} returned no completion")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise IncompleteCompletion(f"Provider did not finish normally: {choice.get('finish_reason', 'unknown')}")
        return choice.get("message", {}).get("content") or ""

    def ask(self, role: str, payload: dict, schema: type[T]) -> T:
        history = []
        for attempt in range(3):
            with self._lock:
                if self.calls >= self.max_calls:
                    raise ProviderError("Model call budget reached. Saved progress can be resumed with a larger budget.")
                self.calls += 1
                self._log_seq += 1
                sequence = self._log_seq
            endpoint, headers, body, protocol = self.request_parts(role, payload, schema, history)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            logfile = self.log_dir / f"{stamp}-{sequence:05d}-{role}.json"
            log = {"role": role, "provider": self.name, "model": self.model, "attempt": attempt + 1, "input": payload}
            try:
                response = httpx.post(endpoint, headers=headers, json=body, timeout=180, follow_redirects=False)
                if response.status_code == 429 or response.status_code >= 500:
                    log["error"] = f"HTTP {response.status_code}"
                    if attempt == 2:
                        raise ProviderError(f"Provider returned HTTP {response.status_code}. Progress is saved; retry later.")
                    delay = response.headers.get("retry-after", "")
                    try:
                        seconds = float(delay)
                    except ValueError:
                        seconds = 10 * (attempt + 1)
                    time.sleep(min(60, max(1, seconds)))
                    continue
                if response.status_code != 200:
                    raise ProviderError(f"Provider returned HTTP {response.status_code}. Check the shared model ID and API key.")
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("Provider response must be an object")
                log["response"] = data
                text = self.completion_text(data, protocol)
                if not isinstance(text, str):
                    raise ValueError("Provider response did not contain text")
                # Accept a complete fenced JSON object, without guessing at malformed JSON.
                fenced = re.fullmatch(r"\s*```(?:json)?\s*(\{.*\})\s*```\s*", text, flags=re.S | re.I)
                if fenced:
                    text = fenced[1]
                try:
                    return schema.model_validate_json(text)
                except ValidationError as exc:
                    history.append(str(exc)[:5000])
                    log["schema_error"] = history[-1]
                    if attempt == 2:
                        raise ProviderError(f"{role} did not produce valid structured output after three attempts") from exc
            except IncompleteCompletion as exc:
                log["error"] = str(exc)
                if attempt == 2:
                    raise
                history.append("The last response was incomplete. Return one complete, concise JSON object matching the schema.")
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                log["error"] = type(exc).__name__
                if attempt == 2:
                    raise ProviderError("Provider request failed. Saved progress can be resumed.") from exc
            finally:
                # Request headers are excluded; payloads and provider responses may be sensitive.
                logfile.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ProviderError("Provider request exhausted its retry budget")


class DeepSeekProvider(ModelProvider):
    """Compatibility constructor for the direct DeepSeek provider."""
    def __init__(self, model: str, api_key: str, log_dir: Path, max_calls: int = 400):
        super().__init__(model, api_key, log_dir, max_calls=max_calls, provider="deepseek")


def provider_configuration(provider: str, model: str | None = None) -> tuple[str, str]:
    if provider not in PROVIDERS:
        raise ProviderError("Choose a provider from the menu")
    prefix = PROVIDERS[provider]["prefix"]
    return model or os.getenv(f"{prefix}_MODEL", ""), os.getenv(f"{prefix}_API_KEY", "")
