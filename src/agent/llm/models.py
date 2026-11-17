"""Model provider.

Pro is used for SQL and analysis. Flash is the fallback and the optional guard
classifier. A scripted `complete(system, user, tier)` callable replaces Gemini
in tests so the suite never touches the network. Retries cover 429/5xx/timeouts;
a still-failing Pro call falls through to Flash.
"""

from __future__ import annotations

import time
from typing import Callable

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential, wait_fixed

from config.settings import Settings


class LLMTransientError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMFatalError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, threshold: int = 3, reset_seconds: float = 60) -> None:
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.failures = 0
        self.open_until = 0.0

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def record_success(self) -> None:
        self.failures = 0

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = now + self.reset_seconds
            self.failures = 0


def is_transient(exc: Exception) -> bool:
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if int(code) in {429, 500, 502, 503, 504}:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    return any(token in text for token in ("429", "500", "502", "503", "504", "timeout", "unavailable", "resource exhausted", "rate limit"))


CompleteFn = Callable[[str, str, str], str]


class ModelProvider:
    def __init__(
        self,
        settings: Settings,
        complete: CompleteFn | None = None,
        *,
        clock: Callable[[], float] = time.time,
        retry_min: float = 0.05,
        retry_max: float = 0.4,
    ) -> None:
        self.settings = settings
        self._complete = complete
        self.clock = clock
        self.retry_min = retry_min
        self.retry_max = retry_max
        self.calls: list[str] = []
        self.fallback_used = False
        self.breaker = CircuitBreaker()
        self._models: dict[str, object] = {}

    def available(self) -> bool:
        return self._complete is not None or bool(self.settings.google_api_key)

    def generate(self, *, tier: str, system: str, user: str) -> str:
        primary = tier if tier in {"pro", "flash"} else "flash"
        if primary == "pro" and not self.breaker.allow(self.clock()):
            self.fallback_used = True
            primary = "flash"
        try:
            text = self._call(primary, system, user)
            if primary == "pro":
                self.breaker.record_success()
            return text
        except Exception as first:
            if primary == "flash":
                raise LLMFatalError(str(first)) from first
            self.fallback_used = True
            self.breaker.record_failure(self.clock())
            try:
                return self._call("flash", system, user)
            except Exception as second:
                raise LLMFatalError(f"Gemini unavailable ({first})") from second

    def _call(self, tier: str, system: str, user: str) -> str:
        wait = wait_fixed(0) if self.retry_min <= 0 else wait_exponential(multiplier=self.retry_min, min=self.retry_min, max=self.retry_max)

        @retry(
            reraise=True,
            stop=stop_after_attempt(3),
            wait=wait,
            retry=retry_if_exception_type(LLMTransientError),
        )
        def attempt() -> str:
            self.calls.append(tier)
            try:
                if self._complete is not None:
                    return self._complete(system, user, tier)
                return self._invoke_gemini(tier, system, user)
            except LLMTransientError:
                raise
            except Exception as exc:
                if is_transient(exc):
                    raise LLMTransientError(str(exc), getattr(exc, "status_code", None)) from exc
                raise

        return attempt()

    def _invoke_gemini(self, tier: str, system: str, user: str) -> str:
        if not self.settings.google_api_key:
            raise LLMFatalError("GOOGLE_API_KEY is not set.")
        model = self._gemini(tier)
        from langchain_core.messages import HumanMessage, SystemMessage

        message = model.invoke([SystemMessage(content=system), HumanMessage(content=user)])
        content = message.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or ""))
            return "".join(parts)
        return str(content)

    def _gemini(self, tier: str):
        if tier in self._models:
            return self._models[tier]
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_id = self.settings.gemini_model_pro if tier == "pro" else self.settings.gemini_model_flash
        model = ChatGoogleGenerativeAI(
            model=model_id,
            api_key=self.settings.google_api_key,
            temperature=0,
            max_retries=0,
        )
        self._models[tier] = model
        return model
