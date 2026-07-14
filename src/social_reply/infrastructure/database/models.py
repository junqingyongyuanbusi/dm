import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    automation_default: Mapped[str] = mapped_column(Text, default="BOT_DRAFT_ONLY")
    status: Mapped[str] = mapped_column(Text, default="CONNECTED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("platform_account_id", "external_user_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    external_user_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("tenant_id", "conversation_key"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"))
    conversation_key: Mapped[str] = mapped_column(Text)
    channel_type: Mapped[str] = mapped_column(Text, default="dm")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ConversationMapping(Base):
    __tablename__ = "conversation_mappings"
    __table_args__ = (
        UniqueConstraint("chatwoot_account_id", "chatwoot_conversation_id"),
        UniqueConstraint("conversation_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    chatwoot_account_id: Mapped[int] = mapped_column(Integer)
    chatwoot_conversation_id: Mapped[int] = mapped_column(Integer)
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(Text)  # inbound / outbound
    sender_type: Mapped[str] = mapped_column(Text)  # contact / agent / bot
    text: Mapped[str | None] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    platform_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawEvent(Base):
    __tablename__ = "raw_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(Text)  # chatwoot / meta / telegram ...
    payload: Mapped[dict] = mapped_column(JSONB)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    processing_status: Mapped[str] = mapped_column(Text, default="PENDING")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NormalizedEvent(Base):
    __tablename__ = "normalized_events"
    __table_args__ = (
        # PLAN.md §十二：多租户去重约束
        UniqueConstraint("tenant_id", "platform", "platform_account_id", "external_event_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    external_event_id: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_events.id"))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AutomationState(Base):
    __tablename__ = "automation_states"
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(Text)  # PLAN.md §六 六状态
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    human_agent_id: Mapped[str | None] = mapped_column(Text)
    last_human_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bot_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_policy: Mapped[str] = mapped_column(Text, default="MANUAL")
    state_changed_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    destination_type: Mapped[str] = mapped_column(Text)
    destination_id: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    category: Mapped[str] = mapped_column(Text)  # state_transition / ingestion / ...
    actor: Mapped[str] = mapped_column(Text)  # system / agent:<id> / bot
    action: Mapped[str] = mapped_column(Text)
    subject_type: Mapped[str] = mapped_column(Text)
    subject_id: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
