"""DeepSeek compatibility adapter.

The generic client in :mod:`stockbrain.llm.openai_compat` does the work; this
module supplies the DeepSeek profile, the settings-shaped constructor, and the
provider-named wrappers the rest of the codebase and the DeepSeek regression
tests import.

Everything DeepSeek-only stays behind this boundary and behind
:data:`stockbrain.llm.profiles.DEEPSEEK` -- ``thinking: {"type": "disabled"}``,
``reasoning_content``, ``insufficient_system_resource``, the
``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens`` split and the
time-of-day pricing. None of it is sent to, or expected from, any other endpoint.

Verified against DeepSeek's current documentation (2026-09-04):

* base ``https://api.deepseek.com``, endpoint ``/chat/completions``,
  ``Authorization: Bearer <key>``, OpenAI-compatible request/response shape
* models: ``deepseek-v4-flash``, ``deepseek-v4-pro``,
  ``deepseek-v4-flash-vision-exp``
* **``thinking`` defaults to ``{"type": "enabled"}``.** The classifier must run
  without it, so ``{"type": "disabled"}`` is sent explicitly on every
  non-thinking call. Omitting the field would silently enable reasoning and
  change both latency and cost.
* JSON mode is ``response_format: {"type": "json_object"}``, and the docs require
  the word "json" to appear in the prompt and recommend showing an example
  schema. Both are enforced rather than left to the prompt author.
* ``usage`` reports ``prompt_tokens``, ``completion_tokens``, ``total_tokens``,
  ``prompt_cache_hit_tokens``, ``prompt_cache_miss_tokens`` and
  ``completion_tokens_details.reasoning_tokens``
* 429 signals the account concurrency limit
* ``finish_reason`` may be ``insufficient_system_resource``, which is a
  server-side capacity condition and is retried like a 5xx
* the server may hold a connection open, emitting blank lines, and closes it if
  inference has not started within ten minutes -- so the read timeout is
  generous but bounded, and JSON decoding tolerates leading whitespace

The API key is held as a ``SecretStr`` and injected into the header at
construction. It is never logged, never included in an exception message, and
never returned to a caller.
"""

from __future__ import annotations

from typing import Any

from stockbrain.config import Settings
from stockbrain.httpclient import ProviderHttpClient
from stockbrain.llm import openai_compat
from stockbrain.llm.base import CompletionResult
from stockbrain.llm.openai_compat import DEFAULT_TIMEOUT_SECONDS, OpenAICompatibleClient
from stockbrain.llm.profiles import DEEPSEEK

__all__ = ["DEEPSEEK_CHAT_PATH", "DeepSeekClient", "extract_json", "parse_completion"]

DEEPSEEK_CHAT_PATH = DEEPSEEK.chat_path


def parse_completion(payload: Any, *, latency_ms: int, attempts: int) -> CompletionResult:
    """Parse a DeepSeek chat-completions response.

    Provider-bound wrapper over the generic parser, kept so the DeepSeek
    regression suite continues to exercise the DeepSeek dialect specifically
    rather than a profile the test itself chose.
    """
    return openai_compat.parse_completion(
        payload, DEEPSEEK, latency_ms=latency_ms, attempts=attempts
    )


def extract_json(content: str) -> Any:
    """Decode a DeepSeek JSON reply, tolerating the wrappers models add."""
    return openai_compat.extract_json(content, provider=DEEPSEEK.name)


class DeepSeekClient(OpenAICompatibleClient):
    """Async DeepSeek client with bounded, classified retries."""

    name = "deepseek"

    def __init__(
        self,
        settings: Settings,
        *,
        http: ProviderHttpClient | None = None,
        max_attempts: int = 3,
    ) -> None:
        super().__init__(
            DEEPSEEK,
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            timeout_seconds=settings.deepseek_timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
            max_attempts=max_attempts,
            http=http,
        )
        self._settings = settings
