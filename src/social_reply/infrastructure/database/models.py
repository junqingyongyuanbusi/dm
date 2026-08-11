import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from social_reply.domain.platform_accounts import ACTIVE_ACCOUNT_STATUS
from social_reply.domain.reply.voice import (
    CANONICAL_VOICE_PREFERENCES,
    CANONICAL_VOICE_PREFERENCES_JSON,
)


class Base(DeclarativeBase):
    pass


_MESSAGE_HISTORY_SEQUENCE = Sequence("messages_history_seq_seq")


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class AdminUser(Base):
    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint("username"),
        UniqueConstraint("tenant_id"),
        UniqueConstraint("tenant_id", "id", name="uq_admin_users_tenant_id_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(Text)
    tenant_id: Mapped[str] = mapped_column(String(64))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    __table_args__ = (
        UniqueConstraint("token_digest"),
        CheckConstraint(
            "(user_id IS NOT NULL) <> (bootstrap_fingerprint IS NOT NULL)",
            name="ck_admin_sessions_single_identity",
        ),
        Index("ix_admin_sessions_user_id", "user_id"),
        Index("ix_admin_sessions_expires_at", "expires_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    token_digest: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE")
    )
    bootstrap_fingerprint: Mapped[str | None] = mapped_column(String(64))
    credential_fingerprint: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlatformApp(Base):
    __tablename__ = "platform_apps"
    __table_args__ = (
        UniqueConstraint("platform_family", "public_id"),
        UniqueConstraint("tenant_id", "platform_family", "external_app_id"),
        Index(
            "uq_platform_apps_meta_route_public_id",
            "public_id",
            unique=True,
            postgresql_where=text("platform_family IN ('meta', 'instagram')"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    platform_family: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    external_app_id: Mapped[str | None] = mapped_column(Text)
    public_id: Mapped[str] = mapped_column(Text)
    credential_ref: Mapped[str] = mapped_column(Text, default="")
    # Application-encrypted envelope shared by API and workers; plaintext never enters PostgreSQL.
    credential_bundle: Mapped[dict | None] = mapped_column(JSONB)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(Text, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlatformAccount(Base):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint("platform", "public_id"),
        UniqueConstraint("tenant_id", "platform", "external_account_id"),
        UniqueConstraint("tenant_id", "id", name="uq_platform_accounts_tenant_id_id"),
        CheckConstraint(
            "platform IN ('telegram', 'facebook', 'instagram', 'whatsapp', 'x', 'feishu', 'email')",
            name="ck_platform_accounts_platform",
        ),
        CheckConstraint(
            "status IN ('active', 'DISABLED')",
            name="ck_platform_accounts_status",
        ),
        CheckConstraint(
            "jsonb_typeof(capability) = 'object'",
            name="ck_platform_accounts_capability_object",
        ),
        Index("ix_platform_accounts_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_apps.id"))
    name: Mapped[str] = mapped_column(Text)
    external_account_id: Mapped[str | None] = mapped_column(Text)
    public_id: Mapped[str | None] = mapped_column(Text)
    credential_ref: Mapped[str | None] = mapped_column(Text)
    webhook_secret_ref: Mapped[str | None] = mapped_column(Text)
    credential_bundle: Mapped[dict | None] = mapped_column(JSONB)
    webhook_secret_bundle: Mapped[dict | None] = mapped_column(JSONB)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    capability: Mapped[dict] = mapped_column(JSONB, default=dict)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    chatwoot_inbox_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    automation_default: Mapped[str] = mapped_column(Text, default="BOT_DRAFT_ONLY")
    status: Mapped[str] = mapped_column(Text, default=ACTIVE_ACCOUNT_STATUS)
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
    __table_args__ = (
        UniqueConstraint("tenant_id", "conversation_key"),
        UniqueConstraint("tenant_id", "id", name="uq_conversations_tenant_id_id"),
        CheckConstraint(
            "decision_generation >= 0",
            name="ck_conversations_decision_generation",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contacts.id"))
    conversation_key: Mapped[str] = mapped_column(Text)
    channel_type: Mapped[str] = mapped_column(Text, default="dm")
    decision_generation: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
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
    __table_args__ = (
        UniqueConstraint("history_seq", name="uq_messages_history_seq"),
        UniqueConstraint("source_outbox_id", name="uq_messages_source_outbox_id"),
        Index("ix_messages_conversation_history", "conversation_id", "history_seq"),
        Index(
            "ix_messages_conversation_decision_generation",
            "conversation_id",
            "decision_generation",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    history_seq: Mapped[int] = mapped_column(
        BigInteger,
        _MESSAGE_HISTORY_SEQUENCE,
        server_default=_MESSAGE_HISTORY_SEQUENCE.next_value(),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"), index=True)
    direction: Mapped[str] = mapped_column(Text)  # inbound / outbound
    sender_type: Mapped[str] = mapped_column(Text)  # contact / agent / bot
    text: Mapped[str | None] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    platform_message_id: Mapped[str | None] = mapped_column(Text, index=True)
    source_outbox_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outbox_messages.id"))
    decision_generation: Mapped[int | None] = mapped_column(BigInteger)
    reply_target: Mapped[dict] = mapped_column(JSONB, default=dict)
    attachments: Mapped[list] = mapped_column(JSONB, default=list)
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RawEvent(Base):
    __tablename__ = "raw_events"
    __table_args__ = (
        CheckConstraint(
            "processing_attempt_count >= 0",
            name="ck_raw_events_processing_attempt_count",
        ),
        Index("ix_raw_events_status_received", "processing_status", "received_at"),
        Index("ix_raw_events_account_received", "platform_account_id", "received_at"),
        Index(
            "ix_raw_events_tenant_status_received",
            "tenant_id",
            "processing_status",
            "received_at",
        ),
        Index(
            "ix_raw_events_processing_due",
            "processing_status",
            "processing_next_attempt_at",
        ),
        Index(
            "uq_raw_events_feishu_webhook_external_event",
            "platform_account_id",
            "external_event_id",
            unique=True,
            postgresql_where=text(
                "source = 'feishu' AND ingress_kind = 'webhook' AND external_event_id IS NOT NULL"
            ),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str | None] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("platform_accounts.id")
    )
    source: Mapped[str] = mapped_column(Text)  # chatwoot / meta / telegram / poll stream
    ingress_kind: Mapped[str] = mapped_column(Text, default="webhook")
    event_namespace: Mapped[str | None] = mapped_column(Text)
    external_event_id: Mapped[str | None] = mapped_column(Text)
    external_conversation_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    context: Mapped[dict] = mapped_column(JSONB, default=dict)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_status: Mapped[str] = mapped_column(Text, default="PENDING")
    processing_claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    processing_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    processing_next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_error_code: Mapped[str | None] = mapped_column(Text)
    processing_last_dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformCheckpoint(Base):
    __tablename__ = "platform_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "platform_account_id",
            "stream",
            "scope_key",
            name="uq_platform_checkpoints_account_stream_scope",
        ),
        CheckConstraint(
            "stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'XCHAT_CONVERSATION', 'EMAIL_IMAP')",
            name="ck_platform_checkpoints_stream",
        ),
        CheckConstraint(
            "(stream = 'XCHAT_CONVERSATION' AND scope_key <> '') OR "
            "(stream IN ('X_LEGACY_DM', 'XCHAT_DISCOVERY', 'EMAIL_IMAP') "
            "AND scope_key = '')",
            name="ck_platform_checkpoints_scope",
        ),
        CheckConstraint("revision >= 0", name="ck_platform_checkpoints_revision"),
        CheckConstraint(
            "(claim_token IS NULL AND claimed_by IS NULL AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claimed_by IS NOT NULL "
            "AND claim_expires_at IS NOT NULL)",
            name="ck_platform_checkpoints_claim",
        ),
        Index(
            "ix_platform_checkpoints_due",
            "stream",
            "next_attempt_at",
            "claim_expires_at",
        ),
        Index("ix_platform_checkpoints_account", "platform_account_id", "stream"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    platform_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_accounts.id", ondelete="CASCADE")
    )
    stream: Mapped[str] = mapped_column(Text)
    scope_key: Mapped[str] = mapped_column(Text, default="")
    cursor: Mapped[str | None] = mapped_column(Text)
    bootstrapped: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(BigInteger, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_by: Mapped[str | None] = mapped_column(Text)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (
        CheckConstraint("mode IN ('POLL', 'BACKFILL')", name="ck_sync_runs_mode"),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'GAPPED', 'FAILED', 'LEASE_LOST')",
            name="ck_sync_runs_status",
        ),
        CheckConstraint(
            "page_count >= 0 AND occurrence_count >= 0",
            name="ck_sync_runs_counts",
        ),
        Index("ix_sync_runs_checkpoint_started", "checkpoint_id", "started_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    checkpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_checkpoints.id", ondelete="CASCADE")
    )
    claim_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="RUNNING")
    cursor_before: Mapped[str | None] = mapped_column(Text)
    cursor_after: Mapped[str | None] = mapped_column(Text)
    resume_token: Mapped[str | None] = mapped_column(Text)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncGap(Base):
    __tablename__ = "sync_gaps"
    __table_args__ = (
        CheckConstraint(
            "gap_type IN ('PAGE_CAP', 'PAGINATION_ERROR', 'DECRYPT_ERROR', "
            "'EMAIL_UIDVALIDITY_CHANGED')",
            name="ck_sync_gaps_type",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'RETRYING', 'RESOLVED')",
            name="ck_sync_gaps_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_sync_gaps_attempt_count"),
        Index("ix_sync_gaps_retry", "status", "next_attempt_at"),
        Index(
            "uq_sync_gaps_active_checkpoint",
            "checkpoint_id",
            unique=True,
            postgresql_where=text("status IN ('OPEN', 'RETRYING')"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    checkpoint_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("platform_checkpoints.id", ondelete="CASCADE")
    )
    sync_run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sync_runs.id", ondelete="CASCADE"))
    gap_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="OPEN")
    cursor_before: Mapped[str | None] = mapped_column(Text)
    candidate_cursor: Mapped[str | None] = mapped_column(Text)
    resume_token: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NormalizedEvent(Base):
    __tablename__ = "normalized_events"
    __table_args__ = (
        # 多租户去重约束
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
    external_conversation_id: Mapped[str | None] = mapped_column(Text)
    event_metadata: Mapped[dict] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AutomationState(Base):
    __tablename__ = "automation_states"
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(Text)
    state_version: Mapped[int] = mapped_column(Integer, default=1)
    human_agent_id: Mapped[str | None] = mapped_column(Text)
    last_human_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_bot_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_policy: Mapped[str] = mapped_column(Text, default="MANUAL")
    state_changed_reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HumanWorkItem(Base):
    __tablename__ = "human_work_items"
    __table_args__ = (
        CheckConstraint(
            "status IN ('WAITING', 'CLAIMED', 'RESOLVED', 'CANCELLED')",
            name="ck_human_work_items_status",
        ),
        CheckConstraint("version >= 1", name="ck_human_work_items_version"),
        CheckConstraint(
            "status <> 'CLAIMED' OR (assigned_actor IS NOT NULL AND claimed_at IS NOT NULL)",
            name="ck_human_work_items_claimed_assignment",
        ),
        CheckConstraint(
            "resolution_evidence IS NULL OR resolution_evidence IN "
            "('REPLY_CORE_CONFIRMED', 'FEISHU_OPERATOR_ATTESTED', "
            "'ADMIN_OPERATOR_ATTESTED', 'SUPERVISOR_OVERRIDE')",
            name="ck_human_work_items_resolution_evidence",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_human_work_items_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_human_work_items_tenant_conversation",
            ondelete="CASCADE",
        ),
        Index(
            "uq_human_work_items_open_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('WAITING', 'CLAIMED')"),
        ),
        Index(
            "ix_human_work_items_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index("ix_human_work_items_assigned_status", "assigned_user_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, default="WAITING")
    reason_code: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    assigned_actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_actor: Mapped[str | None] = mapped_column(Text)
    resolution_evidence: Mapped[str | None] = mapped_column(Text)
    resolution_outbox_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "outbox_messages.id",
            name="fk_human_work_items_resolution_outbox_id",
            ondelete="SET NULL",
        )
    )
    version: Mapped[int] = mapped_column(Integer, default=1)


class TenantFeishuHandoffConfig(Base):
    __tablename__ = "tenant_feishu_handoff_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id"),
        UniqueConstraint("tenant_id", "id", name="uq_tenant_feishu_handoff_configs_tenant_id_id"),
        CheckConstraint("config_version >= 1", name="ck_feishu_handoff_configs_version"),
        CheckConstraint(
            "length(btrim(destination_chat_id)) > 0",
            name="ck_feishu_handoff_configs_chat_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_handoff_configs_tenant_account",
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    feishu_platform_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    destination_chat_id: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    config_version: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    card_locale: Mapped[str] = mapped_column(Text, default="zh_cn", server_default=text("'zh_cn'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FeishuHandoffOperator(Base):
    __tablename__ = "feishu_handoff_operators"
    __table_args__ = (
        UniqueConstraint(
            "feishu_platform_account_id",
            "operator_open_id",
            name="uq_feishu_handoff_operators_account_open_id",
        ),
        CheckConstraint(
            "status IN ('ACTIVE', 'DISABLED')",
            name="ck_feishu_handoff_operators_status",
        ),
        CheckConstraint(
            "length(btrim(operator_open_id)) > 0",
            name="ck_feishu_handoff_operators_open_id",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_handoff_operators_tenant_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "admin_user_id"],
            ["admin_users.tenant_id", "admin_users.id"],
            name="fk_feishu_handoff_operators_tenant_admin_user",
        ),
        Index("ix_feishu_handoff_operators_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    feishu_platform_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    operator_open_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    can_claim: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    can_resolve: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    status: Mapped[str] = mapped_column(Text, default="ACTIVE", server_default=text("'ACTIVE'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class HandoffNotificationIntent(Base):
    __tablename__ = "handoff_notification_intents"
    __table_args__ = (
        UniqueConstraint("public_id"),
        UniqueConstraint("human_work_item_id"),
        UniqueConstraint("provider_uuid"),
        UniqueConstraint("tenant_id", "id", name="uq_handoff_notification_intents_tenant_id_id"),
        CheckConstraint(
            "status IN ('BLOCKED_CONFIG', 'PENDING', 'SENDING', 'SYNCED', "
            "'FAILED', 'NEEDS_REVIEW', 'CANCELLED')",
            name="ck_handoff_notification_intents_status",
        ),
        CheckConstraint(
            "desired_card_state IN ('WAITING', 'CLAIMED', 'RESOLVED', 'CANCELLED')",
            name="ck_handoff_notification_intents_card_state",
        ),
        CheckConstraint(
            "desired_revision >= 1 AND delivered_revision >= 0 "
            "AND delivered_revision <= desired_revision",
            name="ck_handoff_notification_intents_revisions",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_handoff_notification_intents_attempt_count",
        ),
        CheckConstraint(
            "(status = 'SENDING') = "
            "(claim_token IS NOT NULL AND claim_expires_at IS NOT NULL "
            "AND sending_revision IS NOT NULL)",
            name="ck_handoff_notification_intents_sending_lease",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "human_work_item_id"],
            ["human_work_items.tenant_id", "human_work_items.id"],
            name="fk_handoff_notification_intents_tenant_work",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_handoff_notification_intents_tenant_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "notification_config_id"],
            ["tenant_feishu_handoff_configs.tenant_id", "tenant_feishu_handoff_configs.id"],
            name="fk_handoff_notification_intents_tenant_config",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_handoff_notification_intents_tenant_account",
        ),
        Index(
            "ix_handoff_notification_intents_due",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_handoff_notification_intents_tenant_status",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index("ix_handoff_notification_intents_provider_message", "provider_message_id"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    public_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(Text)
    human_work_item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    notification_config_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    config_version: Mapped[int | None] = mapped_column(Integer)
    feishu_platform_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    destination_chat_id: Mapped[str | None] = mapped_column(Text)
    provider_uuid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    provider_message_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, default="BLOCKED_CONFIG", server_default=text("'BLOCKED_CONFIG'")
    )
    desired_card_state: Mapped[str] = mapped_column(
        Text, default="WAITING", server_default=text("'WAITING'")
    )
    desired_revision: Mapped[int] = mapped_column(Integer, default=1, server_default=text("1"))
    delivered_revision: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    action_nonce: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    sending_revision: Mapped[int | None] = mapped_column(Integer)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FeishuCardActionReceipt(Base):
    __tablename__ = "feishu_card_action_receipts"
    __table_args__ = (
        UniqueConstraint(
            "feishu_platform_account_id",
            "provider_event_id",
            name="uq_feishu_card_action_receipts_account_event",
        ),
        CheckConstraint(
            "action IN ('CLAIM', 'RESOLVE')",
            name="ck_feishu_card_action_receipts_action",
        ),
        CheckConstraint(
            "outcome IN ('PROCESSING', 'SUCCEEDED', 'CONFLICT', 'UNAUTHORIZED', 'MAINTENANCE')",
            name="ck_feishu_card_action_receipts_outcome",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "feishu_platform_account_id"],
            ["platform_accounts.tenant_id", "platform_accounts.id"],
            name="fk_feishu_card_action_receipts_tenant_account",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "notification_intent_id"],
            ["handoff_notification_intents.tenant_id", "handoff_notification_intents.id"],
            name="fk_feishu_card_action_receipts_tenant_intent",
        ),
        Index(
            "ix_feishu_card_action_receipts_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    feishu_platform_account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    provider_event_id: Mapped[str] = mapped_column(Text)
    notification_intent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    operator_open_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(
        Text, default="PROCESSING", server_default=text("'PROCESSING'")
    )
    response_payload: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        # Takeover cancellation and recovery sweeps both filter by conversation and status.
        Index("ix_outbox_conversation_status", "conversation_id", "status"),
        Index(
            "ix_outbox_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_outbox_email_bot_sent_account_time",
            "platform_account_id",
            "sent_at",
            "conversation_id",
            postgresql_where=text(
                "status = 'SENT' AND destination_type = 'email_reply' "
                "AND origin_kind = 'DECISION' AND actor_kind = 'BOT'"
            ),
        ),
        CheckConstraint(
            "origin_kind IN ('DECISION', 'DRAFT_APPROVAL', 'MANUAL_REPLY', 'SYSTEM_NOTICE')",
            name="ck_outbox_origin_kind",
        ),
        CheckConstraint(
            "actor_kind IN ('BOT', 'ADMIN_HUMAN', 'SYSTEM')",
            name="ck_outbox_actor_kind",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    platform_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    destination_type: Mapped[str] = mapped_column(Text)
    destination_id: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    reply_to_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "messages.id",
            name="fk_outbox_messages_reply_to_message_id",
            use_alter=True,
        )
    )
    origin_kind: Mapped[str] = mapped_column(Text, default="DECISION")
    actor_kind: Mapped[str] = mapped_column(Text, default="BOT")
    actor_id: Mapped[str | None] = mapped_column(Text)
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


class ProvisioningJob(Base):
    __tablename__ = "provisioning_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key"),
        Index("ix_provisioning_jobs_status_next_attempt", "status", "next_attempt_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text)
    brand_id: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text, default="CONNECT_ACCOUNT")
    actor: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    request: Mapped[dict] = mapped_column(JSONB)
    staging_secret_ref: Mapped[str] = mapped_column(Text, default="")
    # Encrypted staging envelope; cleared atomically when provisioning completes.
    staging_secret: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    current_step: Mapped[str] = mapped_column(Text, default="QUEUED")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(Text)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_accounts.id"))
    platform_app_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("platform_apps.id"))
    result: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_error_code: Mapped[str | None] = mapped_column(Text)
    last_error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionJob(Base):
    __tablename__ = "decision_jobs"
    __table_args__ = (
        Index("ix_decision_jobs_status_next_attempt", "status", "next_attempt_at"),
        Index("ix_decision_jobs_conversation_generation", "conversation_id", "decision_generation"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    raw_event_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_events.id"))
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id"), unique=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("platform_accounts.id"))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    decision_generation: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReplyDecision(Base):
    __tablename__ = "reply_decisions"
    __table_args__ = (
        UniqueConstraint("message_id"),
        Index(
            "ix_reply_decisions_decision_job_id",
            "decision_job_id",
            unique=True,
            postgresql_where=text("decision_job_id IS NOT NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(Text, default="default")
    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id"))
    message_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("messages.id"))
    action: Mapped[str] = mapped_column(Text)  # auto_reply / draft / handoff / ignore
    intent: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(Text, default="low")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reply_text: Mapped[str | None] = mapped_column(Text)
    original_reply_text: Mapped[str | None] = mapped_column(Text)
    final_reply_text: Mapped[str | None] = mapped_column(Text)
    review_action: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_reason: Mapped[str | None] = mapped_column(Text)
    reply_visibility: Mapped[str] = mapped_column(Text, default="public")
    reason_codes: Mapped[list] = mapped_column(JSONB, default=list)
    source: Mapped[str] = mapped_column(Text)  # rule / llm / guard
    prompt_version: Mapped[str | None] = mapped_column(Text)
    state_version_at_decision: Mapped[int | None] = mapped_column(Integer)
    decision_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decision_jobs.id"))
    decision_generation: Mapped[int | None] = mapped_column(BigInteger)
    decision_claim_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("outbox_messages.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[uuid.UUID] = _uuid_pk()
    outbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outbox_messages.id"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    outcome: Mapped[str] = mapped_column(Text)  # SENT / FAILED / CANCELLED / NEEDS_REVIEW
    error_code: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    chatwoot_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published')",
            name="ck_knowledge_documents_status",
        ),
        # 词法检索（BM25 近似）：question 的 tsvector GIN 索引，用于混合检索的关键词一路，
        # 补向量对专有名词（pip/broker/品牌名）召回不足的短板。'simple' 分词器不做词干/停用词，
        # 对多语言与短模板更稳（避免 english 词干把 "pips"→"pip" 误并或丢词）。
        Index(
            "ix_knowledge_documents_question_tsv",
            "question_tsv",
            postgresql_using="gin",
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), default="default", index=True)
    brand_id: Mapped[str] = mapped_column(String(64), default="default")
    platform: Mapped[str | None] = mapped_column(String(32))  # NULL=全平台
    category: Mapped[str | None] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)  # 模板触发问题/关键词
    reply: Mapped[str] = mapped_column(Text)  # 标准回复
    # 生成列：DB 自动维护 question 的 tsvector，导入/更新无需手工计算（DRY）
    question_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', question)", persisted=True)
    )
    status: Mapped[str] = mapped_column(String(16), default="draft", server_default=text("'draft'"))
    is_official_contact: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    source_file: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "content_hash"),
        # 余弦相似度检索用 HNSW 索引
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), default="default")
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True
    )
    content: Mapped[str] = mapped_column(Text)  # 展示/LLM 上下文用（question+reply 拼接）
    # 实际参与 embedding 的文本。非对称检索：只 embed question，与用户 query 同语义空间对齐，
    # 不让 answer 措辞稀释向量（见 importer）。历史行可能为 NULL（旧数据 embed 的是 content）。
    embed_text: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))  # tenant-scoped sha256 idempotency
    embedding_version: Mapped[str] = mapped_column(String(32))  # 如 "text-embedding-3-small"
    embedding: Mapped[list[float]] = mapped_column(Vector(1536))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReplyPrompt(Base):
    """Persist finite brand-voice preferences and their old-Worker text projection.

    WikiFX identity, action semantics, and safety invariants remain code-owned and are never
    stored as editable prompt instructions.
    """

    __tablename__ = "reply_prompts"
    __table_args__ = (UniqueConstraint("tenant_id", "brand_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    brand_id: Mapped[str] = mapped_column(String(64), default="default")
    # Compatibility projection for old Workers. New code compiles this from voice_preferences
    # and never executes database persona text as instructions.
    persona: Mapped[str] = mapped_column(Text)
    voice_preferences: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        default=lambda: CANONICAL_VOICE_PREFERENCES.copy(),
        server_default=text(f"'{CANONICAL_VOICE_PREFERENCES_JSON}'::jsonb"),
    )
    # Every save increments this and records it in reply_decisions.prompt_version.
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
