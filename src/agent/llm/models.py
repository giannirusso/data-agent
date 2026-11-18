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


class LLMConfigError(LLMFatalError):
    """Model id, key, or permission problem. Not retried and not called 'busy'."""

    def __init__(self, message: str, *, model_id: str | None = None) -> None:
        super().__init__(message)
        self.model_id = model_id


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


def is_config_error(exc: Exception) -> bool:
    """404, retired model ids, bad keys, and permission errors. Not a 429/503."""
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if int(code) in {400, 401, 403, 404}:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    markers = (
        "no longer available",
        "not found",
        "not_found",
        "404",
        "invalid api key",
        "api key not valid",
        "api_key_invalid",
        "permission denied",
        "unauthenticated",
        "permission_denied",
    )
    return any(marker in text for marker in markers)


def is_transient(exc: Exception) -> bool:
    if is_config_error(exc):
        return False
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    try:
        if int(code) in {429, 500, 502, 503, 504}:
            return True
    except (TypeError, ValueError):
        pass
    text = str(exc).lower()
    return any(
        token in text
        for token in ("429", "500", "502", "503", "504", "timeout", "resource exhausted", "rate limit", "service unavailable")
    )


def config_message(model_id: str, exc: Exception) -> str:
    return (
        f"Model {model_id} not available: {short_reason(exc)}. "
        "Set GEMINI_MODEL_PRO and GEMINI_MODEL_FLASH in .env."
    )


def short_reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    lowered = text.lower()
    if "no longer available" in lowered:
        return "no longer available to new users"
    if "api key" in lowered or "api_key" in lowered:
        return "invalid API key"
    if "permission" in lowered or "unauthenticated" in lowered:
        return "permission denied"
    if "404" in lowered or "not found" in lowered or "not_found" in lowered:
        return "not found"
    return text[:160] or type(exc).__name__


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
        # One retry on 429/503. A 20s timeout times three retries times two
        # tiers was enough to turn a retired model id into a multi-minute wait.
        self.max_attempts = 2

    def available(self) -> bool:
        return self._complete is not None or bool(self.settings.google_api_key)

    def generate(
        self,
        *,
        tier: str,
        system: str,
        user: str,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        primary = tier if tier in {"pro", "flash"} else "flash"
        if primary == "pro" and not self.breaker.allow(self.clock()):
            self.fallback_used = True
            primary = "flash"
        try:
            text = self._call(
                primary,
                system,
                user,
                thinking_level=thinking_level,
                max_output_tokens=max_output_tokens,
            )
            if primary == "pro":
                self.breaker.record_success()
            return text
        except LLMConfigError:
            if primary == "flash" or self._same_model():
                raise
            self.fallback_used = True
            return self._call(
                "flash",
                system,
                user,
                thinking_level=thinking_level,
                max_output_tokens=max_output_tokens,
            )
        except Exception as first:
            if primary == "flash" or self._same_model():
                if isinstance(first, LLMTransientError):
                    raise LLMFatalError("The model is busy right now.") from first
                raise LLMFatalError(str(first)) from first
            self.fallback_used = True
            self.breaker.record_failure(self.clock())
            try:
                return self._call(
                    "flash",
                    system,
                    user,
                    thinking_level=thinking_level,
                    max_output_tokens=max_output_tokens,
                )
            except LLMConfigError:
                raise
            except Exception as second:
                raise LLMFatalError("The model is busy right now.") from second

    def _same_model(self) -> bool:
        return self.settings.gemini_model_pro == self.settings.gemini_model_flash

    def _model_id(self, tier: str) -> str:
        if tier == "pro":
            return self.settings.gemini_model_pro
        return self.settings.gemini_model_flash

    def _call(
        self,
        tier: str,
        system: str,
        user: str,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        wait = wait_fixed(0) if self.retry_min <= 0 else wait_exponential(multiplier=self.retry_min, min=self.retry_min, max=self.retry_max)

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_attempts),
            wait=wait,
            retry=retry_if_exception_type(LLMTransientError),
        )
        def attempt() -> str:
            self.calls.append(tier)
            try:
                if self._complete is not None:
                    return self._complete(system, user, tier)
                return self._invoke_gemini(
                    tier,
                    system,
                    user,
                    thinking_level=thinking_level,
                    max_output_tokens=max_output_tokens,
                )
            except LLMConfigError:
                raise
            except LLMTransientError:
                raise
            except Exception as exc:
                if is_config_error(exc):
                    raise LLMConfigError(config_message(self._model_id(tier), exc), model_id=self._model_id(tier)) from exc
                if is_transient(exc):
                    raise LLMTransientError(str(exc), getattr(exc, "status_code", None)) from exc
                raise

        return attempt()

    def _invoke_gemini(
        self,
        tier: str,
        system: str,
        user: str,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
    ) -> str:
        if not self.settings.google_api_key:
            raise LLMFatalError("GOOGLE_API_KEY is not set.")
        model = self._gemini(tier)
        from langchain_core.messages import HumanMessage, SystemMessage

        kwargs: dict = {"timeout": self.settings.gemini_timeout_seconds}
        if thinking_level:
            kwargs["thinking_level"] = thinking_level
        if max_output_tokens:
            kwargs["max_output_tokens"] = max_output_tokens
        message = model.invoke(
            [SystemMessage(content=system), HumanMessage(content=user)],
            **kwargs,
        )
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
            timeout=self.settings.gemini_timeout_seconds,
            # Gemini 3 defaults to a deep thinking level. "low" is the base.
            # SQL calls override this with "minimal" (see generate_sql).
            thinking_level="low",
            max_output_tokens=1024,
        )
        self._models[tier] = model
        return model
