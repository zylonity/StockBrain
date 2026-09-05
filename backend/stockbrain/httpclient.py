"""Shared async HTTP client for external providers.

Three properties matter here more than convenience:

1. **Retries are opt-in per request, never global.** ``retry_safe`` defaults to
   ``False``.  A GET may be retried; a broker mutation may not, and there is
   deliberately no middleware that could ever decide otherwise on its own.
2. **Rate-limit headers are consumed and recorded**, and an optional token
   bucket enforces a client-side ceiling.  SEC EDGAR blocks an IP for ~10
   minutes after exceeding 10 requests/second, so staying under the limit is
   not optional.
3. **Errors are classified**, because the caller's correct response differs:
   bad credentials must not be retried, a missing entitlement must degrade a
   subsystem rather than crash it, and only transient failures earn a retry.

Credentials are held as :class:`~pydantic.SecretStr` and injected into headers
at request time; they never appear in an exception message or a log line.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import httpx

from stockbrain.errors import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderRateLimited,
    ProviderResponseError,
    ProviderUnavailable,
)
from stockbrain.logging import get_logger
from stockbrain.observability.metrics import METRICS

__all__ = ["ProviderHttpClient", "RateLimitSnapshot", "TokenBucket"]

log = get_logger(__name__)

_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class RateLimitSnapshot:
    """Rate-limit state parsed from response headers.

    Header spellings differ per provider (Trading 212 uses ``x-ratelimit-*``
    lowercase with a ``period``; Alpaca uses ``X-RateLimit-*`` with a UNIX
    ``reset``), so lookups are case-insensitive and every field is optional.
    """

    limit: int | None = None
    remaining: int | None = None
    used: int | None = None
    reset: int | None = None
    period: str | None = None
    raw: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> RateLimitSnapshot:
        lowered = {key.lower(): value for key, value in headers.items()}
        captured = {key: value for key, value in lowered.items() if key.startswith("x-ratelimit")}

        def as_int(name: str) -> int | None:
            value = lowered.get(name)
            if value is None:
                return None
            try:
                return int(float(value))
            except ValueError:
                return None

        return cls(
            limit=as_int("x-ratelimit-limit"),
            remaining=as_int("x-ratelimit-remaining"),
            used=as_int("x-ratelimit-used"),
            reset=as_int("x-ratelimit-reset"),
            period=lowered.get("x-ratelimit-period"),
            raw=captured,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "used": self.used,
            "reset": self.reset,
            "period": self.period,
        }


class TokenBucket:
    """Simple async token bucket for client-side rate limiting.

    Used to stay strictly under a provider's documented ceiling rather than
    discovering it by being blocked.
    """

    def __init__(self, rate_per_second: float, burst: int | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        self._rate = rate_per_second
        self._capacity = float(burst if burst is not None else max(1.0, rate_per_second))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        """Take a token if one is free, without waiting.

        The order path needs this: waiting for a token is fine, but it has to
        happen *before* the transaction that records "bytes may have left", so a
        limiter denial stays provably a pre-send condition rather than becoming
        part of the unknown window.
        """
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


class ProviderHttpClient:
    """A per-provider HTTP client with classified errors and optional retries."""

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
        rate_limiter: TokenBucket | None = None,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.5,
        backoff_max_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
        refine_error: Callable[[httpx.Response, Exception], Exception] | None = None,
    ) -> None:
        self.provider = provider
        self._rate_limiter = rate_limiter
        self._max_attempts = max(1, max_attempts)
        self._backoff_base = backoff_base_seconds
        self._backoff_max = backoff_max_seconds
        self._refine_error = refine_error
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers=dict(headers or {}),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
        )
        self.last_rate_limit: RateLimitSnapshot | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Request execution
    # ------------------------------------------------------------------
    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        headers: Mapping[str, str] | None = None,
        retry_safe: bool = False,
        max_attempts: int | None = None,
    ) -> Any:
        """Perform a request and return the decoded JSON body.

        ``retry_safe`` must be set explicitly by the caller and asserts that
        repeating this exact request cannot cause a side effect.  It defaults to
        ``False`` so that no request is ever retried by accident.
        """
        attempts = max_attempts if max_attempts is not None else self._max_attempts
        if not retry_safe:
            attempts = 1

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self._rate_limiter is not None:
                await self._rate_limiter.acquire()
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=dict(params) if params else None,
                    json=json_body,
                    headers=dict(headers) if headers else None,
                )
            except httpx.HTTPError as exc:
                # A transport failure on a retry-unsafe request is the caller's
                # problem to classify; this client never guesses.
                METRICS.inc(
                    "stockbrain_provider_errors_total",
                    labels={"provider": self.provider, "kind": "transport"},
                )
                last_error = ProviderUnavailable(
                    f"{self.provider}: transport failure ({type(exc).__name__})"
                )
                if attempt >= attempts:
                    raise last_error from exc
                await self._sleep_backoff(attempt)
                continue

            self.last_rate_limit = RateLimitSnapshot.from_headers(response.headers)
            self._record_rate_limit()

            if response.is_success:
                return self._decode(response)

            error = self._classify(response)
            if isinstance(error, ProviderRateLimited | ProviderUnavailable) and attempt < attempts:
                last_error = error
                await self._sleep_backoff(attempt, hint=getattr(error, "retry_after_seconds", None))
                continue
            raise error

        assert last_error is not None
        raise last_error

    async def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        max_attempts: int | None = None,
    ) -> Any:
        """GET with bounded retries.  Safe by definition: GET has no side effects."""
        return await self.request_json(
            "GET",
            url,
            params=params,
            headers=headers,
            retry_safe=True,
            max_attempts=max_attempts,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _decode(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderResponseError(
                f"{self.provider}: response was not valid JSON "
                f"(status {response.status_code}, content-type "
                f"{response.headers.get('content-type', 'unknown')!r})"
            ) from exc

    def _classify(self, response: httpx.Response) -> Exception:
        """Map a failed response onto the exception the caller must react to.

        A provider-specific ``refine_error`` hook may narrow the result, and
        only that: it is given the already-classified error and may return a
        different *classification*, never a different retry decision.  Alpaca
        needs it because it answers HTTP 403 both for a bad credential and for
        a feed outside the account's plan, and those demand opposite responses.
        """
        error = self._classify_by_status(response)
        if self._refine_error is None:
            return error
        try:
            return self._refine_error(response, error)
        except Exception:  # pragma: no cover - a hook must never mask the error
            log.warning("provider_error_refinement_failed", provider=self.provider)
            return error

    def _classify_by_status(self, response: httpx.Response) -> Exception:
        status = response.status_code
        # The body may echo request parameters; only a short excerpt is kept and
        # it is never interpolated into a log line by this client.
        excerpt = response.text[:300] if response.text else ""
        METRICS.inc(
            "stockbrain_provider_errors_total",
            labels={"provider": self.provider, "kind": str(status)},
        )

        if status in (401, 403):
            return ProviderAuthError(f"{self.provider}: authentication rejected (HTTP {status})")
        if status == 402:
            return ProviderEntitlementError(
                f"{self.provider}: subscription does not cover this resource (HTTP 402)"
            )
        if status == 429:
            retry_after = response.headers.get("retry-after")
            hint: float | None = None
            if retry_after:
                try:
                    hint = float(retry_after)
                except ValueError:
                    hint = None
            return ProviderRateLimited(
                f"{self.provider}: rate limited (HTTP 429)", retry_after_seconds=hint
            )
        if status in _RETRYABLE_STATUS or status >= 500:
            return ProviderUnavailable(f"{self.provider}: HTTP {status}")
        return ProviderResponseError(f"{self.provider}: HTTP {status}: {excerpt}")

    def _record_rate_limit(self) -> None:
        snapshot = self.last_rate_limit
        if snapshot is None or snapshot.remaining is None:
            return
        METRICS.set(
            "stockbrain_provider_rate_limit_remaining",
            float(snapshot.remaining),
            labels={"provider": self.provider},
        )

    async def _sleep_backoff(self, attempt: int, hint: float | None = None) -> None:
        if hint is not None:
            delay = min(hint, self._backoff_max)
        else:
            delay = min(self._backoff_base * (2 ** (attempt - 1)), self._backoff_max)
        # Full jitter: synchronised retries across workers are worse than slow ones.
        await asyncio.sleep(random.uniform(0, delay))  # noqa: S311 - jitter, not crypto
