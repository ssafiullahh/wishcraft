"""Provider adapter with bounded retries and an optional secondary provider (concept 9)."""

import json
import os
import time
from typing import Any

import httpx

from app.observability import record_event, workflow_span


class LLMError(RuntimeError):
    """Safe provider/workflow error; never includes credentials or raw provider bodies."""


class LLMProvider:
    """Keep prompt/workflow logic independent from Ollama and compatible APIs."""

    MAX_ATTEMPTS = 3

    def __init__(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        allow_fallback: bool = True,
    ) -> None:
        self.provider = (provider or os.getenv("LLM_PROVIDER", "ollama")).lower()
        self.model = model or os.getenv("LLM_MODEL", "qwen2.5:7b")
        self.base_url = (base_url or os.getenv(
            "LLM_BASE_URL",
            "http://ollama:11434" if self.provider == "ollama" else "https://api.openai.com/v1",
        )).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
        self.allow_fallback = allow_fallback

    def complete(self, system: str, prompt: str, *, json_mode: bool = False, json_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        try:
            return self._complete_with_retries(system, prompt, json_mode=json_mode, json_schema=json_schema)
        except LLMError as primary_error:
            fallback_name = os.getenv("LLM_FALLBACK_PROVIDER", "").strip()
            record_event("llm.primary_failed", provider=self.provider, model=self.model,
                         error_type=type(primary_error.__cause__).__name__ if primary_error.__cause__ else type(primary_error).__name__,
                         fallback_configured=bool(fallback_name))
            if not self.allow_fallback or not fallback_name:
                raise

            # Concept 9: fallback runs only after the primary's bounded retries fail;
            # it has its own two-attempt limit and cannot recursively start more fallbacks.
            fallback = LLMProvider(
                provider=fallback_name,
                model=os.getenv("LLM_FALLBACK_MODEL") or self.model,
                base_url=os.getenv("LLM_FALLBACK_BASE_URL") or (
                    "http://ollama:11434" if fallback_name.lower() == "ollama" else "https://api.openai.com/v1"
                ),
                api_key=os.getenv("LLM_FALLBACK_API_KEY", ""),
                allow_fallback=False,
            )
            try:
                content, metadata = fallback._complete_with_retries(system, prompt, json_mode=json_mode, json_schema=json_schema)
            except LLMError as fallback_error:
                record_event("llm.fallback_failed", provider=fallback.provider, model=fallback.model,
                             error_type=type(fallback_error.__cause__).__name__ if fallback_error.__cause__ else type(fallback_error).__name__)
                raise LLMError("Primary and fallback LLM requests failed.") from fallback_error
            metadata.update({"fallback_used": True, "primary_provider": self.provider, "primary_model": self.model})
            record_event("llm.fallback_succeeded", primary_provider=self.provider, provider=fallback.provider, model=fallback.model)
            return content, metadata

    def _complete_with_retries(self, system: str, prompt: str, *, json_mode: bool, json_schema: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "ollama":
            url = f"{self.base_url}/api/chat"
            body: dict[str, Any] = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0.2},
            }
            if json_mode:
                body["format"] = json_schema or "json"
        else:
            url = f"{self.base_url}/chat/completions"
            body = {
                "model": self.model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                "temperature": 0.2,
                "response_format": ({
                    "type": "json_schema",
                    "json_schema": {"name": "wishcraft_occasions", "strict": True, "schema": json_schema},
                } if json_schema else {"type": "json_object"}) if json_mode else None,
            }
            body = {key: value for key, value in body.items() if value is not None}

        started = time.perf_counter()
        error: Exception | None = None
        with workflow_span("llm.request", {"llm.provider": self.provider, "llm.model": self.model}) as span:
            for attempt in range(self.MAX_ATTEMPTS):
                try:
                    with httpx.Client(timeout=self.timeout) as client:
                        response = client.post(url, headers=headers, json=body)
                        response.raise_for_status()
                    data = response.json()
                    content = data.get("message", {}).get("content") if self.provider == "ollama" else data["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or not content.strip():
                        raise LLMError("The model returned an empty response.")
                    usage = data.get("prompt_eval_count", 0) + data.get("eval_count", 0) if self.provider == "ollama" else data.get("usage", {}).get("total_tokens")
                    elapsed = round((time.perf_counter() - started) * 1000)
                    if span:
                        span.set_attribute("llm.duration_ms", elapsed)
                        span.set_attribute("llm.retry_count", attempt)
                        if isinstance(usage, int):
                            span.set_attribute("llm.token_count", usage)
                    return content.strip(), {"provider": self.provider, "model": self.model, "duration_ms": elapsed, "tokens": usage, "retry_count": attempt}
                except (httpx.HTTPError, KeyError, IndexError, ValueError, LLMError) as exc:
                    error = exc
                    if attempt < self.MAX_ATTEMPTS - 1:
                        # Short exponential backoff is enough for transient local/API hiccups.
                        time.sleep(0.2 * (2 ** attempt))
            if span:
                span.set_attribute("error.type", type(error).__name__ if error else "unknown")
        raise LLMError(f"LLM request failed ({self.provider}). Check the server-side provider configuration.") from error


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse only a JSON object; refuse prose that would bypass the response schema."""
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        stripped = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LLMError("The model returned invalid structured data.") from exc
    if not isinstance(value, dict):
        raise LLMError("The model response must be a JSON object.")
    return value
