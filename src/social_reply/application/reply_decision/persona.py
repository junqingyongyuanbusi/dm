"""租户品牌语气、风格与本地化偏好段的读取与保存。

可编辑内容存在 PostgreSQL 而不是环境变量，因为 Worker 跑决策、API 跑后台，两个进程必须看到
同一份内容；WikiFX 身份、动作语义、领域事实边界与安全规则由代码内不可变契约负责。
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from social_reply.domain.reply.openai_client import DEFAULT_PERSONA
from social_reply.infrastructure.database import models

PERSONA_MAX_CHARS = 4000


@dataclass(frozen=True)
class ResolvedPersona:
    text: str
    revision: int | None

    @property
    def is_default(self) -> bool:
        return self.revision is None


async def load_persona(session: AsyncSession, tenant_id: str, brand_id: str) -> ResolvedPersona:
    row = (
        await session.execute(
            select(models.ReplyPrompt.persona, models.ReplyPrompt.revision).where(
                models.ReplyPrompt.tenant_id == tenant_id,
                models.ReplyPrompt.brand_id == brand_id,
            )
        )
    ).first()
    if row is None or not (row.persona or "").strip():
        return ResolvedPersona(text=DEFAULT_PERSONA, revision=None)
    return ResolvedPersona(text=row.persona, revision=row.revision)


def prompt_version_label(base: str, persona: ResolvedPersona) -> str:
    """把生效的人设修订号编进 reply_decisions.prompt_version，便于回溯某条回复的来源。"""
    return base if persona.is_default else f"{base}#r{persona.revision}"


def validate_persona(text: str) -> str:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("persona_required")
    if len(cleaned) > PERSONA_MAX_CHARS:
        raise ValueError("persona_too_long")
    return cleaned
