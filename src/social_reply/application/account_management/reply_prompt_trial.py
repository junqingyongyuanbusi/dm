import hashlib
import logging
import time
from contextlib import suppress
from dataclasses import dataclass

import redis.asyncio as aioredis

from social_reply.application.account_management.reply_prompt_policy import (
    ReplyBusinessPromptScopeError,
    load_current_reply_business_prompt,
)
from social_reply.application.reply_decision.runner import _get_llm
from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.llm import LLMContext
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)

REPLY_PROMPT_TRIAL_RATE_LIMIT = 5
REPLY_PROMPT_TRIAL_WINDOW_SECONDS = 60
REPLY_PROMPT_TRIAL_INPUT_MAX_CHARS = 4000


class ReplyBusinessPromptTrialValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ReplyBusinessPromptTrialRateLimited(RuntimeError):
    pass


class ReplyBusinessPromptTrialUnavailable(RuntimeError):
    pass


class ReplyBusinessPromptTrialExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplyBusinessPromptTrialResult:
    action: str
    reply_text: str
    intent: str | None
    risk_level: str
    confidence: float
    reason_codes: tuple[str, ...]
    duration_ms: int


def _trial_actor_digest(actor: str) -> str:
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()[:32]


def _trial_rate_limit_key(tenant_id: str, actor: str) -> str:
    return f"reply-business-prompt:trial:{tenant_id}:{_trial_actor_digest(actor)}"


def _normalize_trial_input(input_text: str) -> str:
    if not isinstance(input_text, str):
        raise ReplyBusinessPromptTrialValidationError("reply_prompt_trial_input_invalid")
    normalized_input = input_text.strip()
    if not normalized_input:
        raise ReplyBusinessPromptTrialValidationError("text_required")
    if len(normalized_input) > REPLY_PROMPT_TRIAL_INPUT_MAX_CHARS:
        raise ReplyBusinessPromptTrialValidationError("reply_prompt_trial_input_too_long")
    return normalized_input


async def _consume_trial_rate_limit(*, tenant_id: str, actor: str) -> None:
    redis_client = aioredis.from_url(get_settings().redis_url)
    try:
        rate_limit_key = _trial_rate_limit_key(tenant_id, actor)
        request_count = int(await redis_client.incr(rate_limit_key))
        if request_count == 1:
            expiration_set = await redis_client.expire(
                rate_limit_key,
                REPLY_PROMPT_TRIAL_WINDOW_SECONDS,
            )
            if not expiration_set:
                raise ReplyBusinessPromptTrialUnavailable(
                    "reply_prompt_trial_rate_limit_unavailable"
                )
        if request_count > REPLY_PROMPT_TRIAL_RATE_LIMIT:
            raise ReplyBusinessPromptTrialRateLimited(
                "reply_prompt_trial_rate_limited"
            )
    except (ReplyBusinessPromptTrialRateLimited, ReplyBusinessPromptTrialUnavailable):
        raise
    except Exception as exc:
        raise ReplyBusinessPromptTrialUnavailable(
            "reply_prompt_trial_rate_limit_unavailable"
        ) from exc
    finally:
        with suppress(Exception):
            await redis_client.aclose()


async def run_reply_business_prompt_trial(
    *,
    tenant_id: str,
    brand_id: str,
    input_text: str,
    actor: str,
) -> ReplyBusinessPromptTrialResult:
    normalized_input = _normalize_trial_input(input_text)
    if not actor:
        raise ReplyBusinessPromptTrialValidationError("reply_prompt_trial_actor_required")

    await _consume_trial_rate_limit(tenant_id=tenant_id, actor=actor)
    async with get_session_factory()() as session:
        try:
            resolved_prompt = await load_current_reply_business_prompt(
                session,
                tenant_id,
                brand_id,
            )
        except ReplyBusinessPromptScopeError:
            raise

    started_at = time.monotonic()
    try:
        decision = await _get_llm().decide(
            LLMContext(
                text=redact_pii(normalized_input),
                conversation_key=(
                    f"trial:{tenant_id}:{brand_id}:{_trial_actor_digest(actor)}"
                ),
                business_prompt=resolved_prompt.instructions,
            )
        )
    except Exception as exc:
        logger.warning(
            "Reply business prompt trial model call failed tenant=%s brand=%s",
            tenant_id,
            brand_id,
        )
        raise ReplyBusinessPromptTrialExecutionError(
            "reply_prompt_trial_execution_failed"
        ) from exc

    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    return ReplyBusinessPromptTrialResult(
        action=decision.action.value,
        reply_text=redact_pii(decision.reply_text or ""),
        intent=decision.intent,
        risk_level=decision.risk_level.value,
        confidence=decision.confidence,
        reason_codes=tuple(decision.reason_codes),
        duration_ms=duration_ms,
    )
