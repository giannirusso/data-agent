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


class LLMTimeoutError(LLMFatalError):
    """The call or the turn budget ran out. This is not a 429."""


class LLMRateLimitError(LLMFatalError):
    """Gemini returned 429. retry_after is seconds, when the API provided one."""

    def __init__(self, message: str = "Gemini rate limit (429)", *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


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
        "invalid_argument",
        "invalid argument",
        "unauthenticated",
    )
    if any(marker in text for marker in markers):
        return True
    import re

    return re.search(r"\b(400|401|403|404)\b", text) is not None


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
    if any(token in text for token in ("timeout", "timed out", "deadline exceeded")):
        return False
    return any(
        token in text
        for token in ("429", "500", "502", "503", "504", "resource exhausted", "rate limit", "service unavailable")
    )


def _walk(exc: BaseException | None):
    current = exc
    seen: list[BaseException] = []
    while current is not None and current not in seen:
        yield current
        seen.append(current)
        current = current.__cause__ or current.__context__


def _is_rate_limit(exc: BaseException) -> bool:
    for item in _walk(exc):
        code = getattr(item, "status_code", None) or getattr(item, "code", None)
        try:
            if int(code) == 429:
                return True
        except (TypeError, ValueError):
            pass
        text = str(item).lower()
        if "429" in text or "rate limit" in text or "resource exhausted" in text:
            return True
    return False


def _retry_after_seconds(exc: BaseException) -> float | None:
    import re

    for item in _walk(exc):
        for attr in ("retry_after", "retry_after_seconds"):
            raw = getattr(item, attr, None)
            if raw is None:
                continue
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                continue
        match = re.search(r"retry[- ]after[:\s]+(\d+(?:\.\d+)?)", str(item), re.IGNORECASE)
        if not match:
            match = re.search(r"retry in (\d+(?:\.\d+)?)\s*s", str(item), re.IGNORECASE)
        if match:
            return max(0.0, float(match.group(1)))
    return None


def _thinking_rejected(exc: BaseException) -> bool:
    for item in _walk(exc):
        text = str(item).lower()
        if "thinking" in text and ("not supported" in text or "invalid_argument" in text or "invalid argument" in text):
            return True
    return False


def _is_timeout(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: list[BaseException] = []
    while current is not None and current not in seen:
        if any(token in str(current).lower() for token in ("timeout", "timed out", "deadline exceeded")):
            return True
        seen.append(current)
        current = current.__cause__ or current.__context__
    return False


def config_message(model_id: str, exc: Exception) -> str:
    return f"Model {model_id} rejected the request: {short_reason(exc)}."


def short_reason(exc: Exception) -> str:
    text = " ".join(str(exc).split())
    lowered = text.lower()
    if "thinking" in lowered and "not supported" in lowered:
        return "thinking level is not supported for this model"
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
        self.llm_calls = 0
        self.max_llm_calls = 0
        self.turn_started = 0.0
        self.turn_budget_s = 0.0

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
        max_attempts: int | None = None,
    ) -> str:
        primary = tier if tier in {"pro", "flash"} else "flash"
        if self.max_llm_calls and self.llm_calls >= self.max_llm_calls:
            raise LLMTimeoutError("The model call cap for this turn was reached. Try `/retry`.")
        if self.turn_started and self.turn_budget_s:
            if (time.perf_counter() - self.turn_started) > self.turn_budget_s:
                raise LLMTimeoutError("The turn timed out before the model answered. Try `/retry`.")
        self.llm_calls += 1
        if primary == "pro" and not self.breaker.allow(self.clock()):
            self.fallback_used = True
            primary = "flash"
        level = thinking_level if thinking_level is not None else (self.settings.gemini_thinking_level or None)
        if isinstance(level, str) and not level.strip():
            level = None
        try:
            try:
                text = self._call(
                    primary,
                    system,
                    user,
                    thinking_level=level,
                    max_output_tokens=max_output_tokens,
                    max_attempts=max_attempts,
                )
            except LLMConfigError as exc:
                if level and _thinking_rejected(exc):
                    text = self._call(
                        primary,
                        system,
                        user,
                        thinking_level=None,
                        max_output_tokens=max_output_tokens,
                        max_attempts=1,
                        apply_thinking=False,
                    )
                else:
                    raise
            if primary == "pro":
                self.breaker.record_success()
            return text
        except LLMTimeoutError:
            raise
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
            if _is_timeout(first):
                raise LLMTimeoutError("The model timed out. Try `/retry`.") from first
            if primary == "flash" or self._same_model():
                if _is_rate_limit(first):
                    raise LLMRateLimitError(retry_after=_retry_after_seconds(first)) from first
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
                if _is_timeout(second):
                    raise LLMTimeoutError("The model timed out. Try `/retry`.") from second
                if _is_rate_limit(second):
                    raise LLMRateLimitError(retry_after=_retry_after_seconds(second)) from second
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
        max_attempts: int | None = None,
        apply_thinking: bool = True,
    ) -> str:
        wait = wait_fixed(0) if self.retry_min <= 0 else wait_exponential(multiplier=self.retry_min, min=self.retry_min, max=self.retry_max)
        attempts = max_attempts or self.max_attempts
        if not apply_thinking:
            thinking_level = None

        @retry(
            reraise=True,
            stop=stop_after_attempt(attempts),
            wait=wait,
            retry=retry_if_exception_type(LLMTransientError),
        )
        def attempt() -> str:
            self.calls.append(tier)
            try:
                if self._complete is not None:
                    return self._scripted(tier, system, user, thinking_level)
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
                if _is_timeout(exc):
                    raise LLMTimeoutError("The model timed out. Try `/retry`.") from exc
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

    def _scripted(self, tier: str, system: str, user: str, thinking_level: str | None) -> str:
        import inspect

        signature = inspect.signature(self._complete)
        parameters = list(signature.parameters.values())
        variadic = any(item.kind == inspect.Parameter.VAR_POSITIONAL for item in parameters)
        positional = [
            item
            for item in parameters
            if item.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        if variadic or len(positional) >= 4:
            return self._complete(system, user, tier, thinking_level)
        return self._complete(system, user, tier)

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
            # Thinking level is sent per request so a rejection can be retried
            # with the parameter omitted. gemini-3.8-flash rejects "minimal".
            max_output_tokens=2048,
        )
        self._models[tier] = model
        return model
