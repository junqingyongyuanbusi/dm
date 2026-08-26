"""separate draft review delivery and persist compact RAG provenance

Revision ID: a7c3e9d1b624
Revises: f2d9c4b8e631
Create Date: 2026-08-25
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "a7c3e9d1b624"
down_revision = "f2d9c4b8e631"
branch_labels = None
depends_on = None


_KNOWN_GLOBAL_BRAND_TERMS = (
    "WikiFX",
    "Meta",
    "Google",
    "Telegram",
    "WhatsApp",
    "Facebook",
    "Instagram",
    "Feishu",
    "OpenAI",
)


def _knowledge_revision_hash(content: str, protected_values: list[str]) -> str:
    normalized = tuple(
        sorted(
            dict.fromkeys(value.strip() for value in protected_values if value.strip()),
            key=lambda value: (value.casefold(), value),
        )
    )
    if not normalized:
        payload = content
    else:
        policy = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        payload = f"knowledge-policy-v1\0{content}\0{policy}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _rewrite_knowledge_chunk_hashes(*, include_policy: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT chunk.id, chunk.tenant_id, chunk.content, document.protected_values "
            "FROM knowledge_chunks AS chunk "
            "JOIN knowledge_documents AS document "
            "ON document.tenant_id = chunk.tenant_id "
            "AND document.id = chunk.document_id "
            "ORDER BY chunk.tenant_id, chunk.id"
        )
    ).mappings()
    updates: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for row in rows:
        protected_values = row["protected_values"] if include_policy else []
        if not isinstance(protected_values, list) or any(
            not isinstance(value, str) for value in protected_values
        ):
            raise RuntimeError("knowledge protected values are not a string array")
        content_hash = _knowledge_revision_hash(row["content"], protected_values)
        identity = (row["tenant_id"], content_hash)
        if identity in identities:
            raise RuntimeError("knowledge hash rewrite would create a tenant duplicate")
        identities.add(identity)
        updates.append({"chunk_id": row["id"], "content_hash": content_hash})

    if updates:
        bind.execute(
            sa.text(
                "UPDATE knowledge_chunks SET content_hash = :content_hash "
                "WHERE id = :chunk_id"
            ),
            updates,
        )


def upgrade() -> None:
    op.add_column("reply_decisions", sa.Column("review_outbox_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_reply_decisions_review_outbox_id",
        "reply_decisions",
        "outbox_messages",
        ["review_outbox_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_reply_decisions_review_outbox_id",
        "reply_decisions",
        ["review_outbox_id"],
    )
    op.add_column(
        "reply_decisions",
        sa.Column("decision_release_sha", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("retrieval_policy_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column("selector_version", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "reply_decisions",
        sa.Column(
            "rag_evidence",
            postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
        ),
    )
    op.add_column(
        "knowledge_documents",
        sa.Column(
            "protected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_reply_decisions_rag_evidence_object",
        "reply_decisions",
        "rag_evidence IS NULL "
        "OR jsonb_typeof(rag_evidence) IS NOT DISTINCT FROM 'object'",
    )
    op.create_check_constraint(
        "ck_knowledge_documents_protected_values_array",
        "knowledge_documents",
        "jsonb_typeof(protected_values) IS NOT DISTINCT FROM 'array'",
    )

    # Legacy outbox_id mixed the private Chatwoot note with the later customer-facing
    # approval. Only authority-tagged human approvals are review deliveries; DECISION/BOT
    # rows remain private-note or automatic-delivery compatibility links.
    op.execute(
        sa.text(
            "UPDATE reply_decisions AS decision "
            "SET review_outbox_id = decision.outbox_id, outbox_id = NULL "
            "FROM outbox_messages AS outbox "
            "WHERE decision.outbox_id = outbox.id "
            "AND outbox.origin_kind = 'DRAFT_APPROVAL' "
            "AND outbox.actor_kind = 'ADMIN_HUMAN'"
        )
    )

    # Keep the backfill deliberately finite and exact-case. These are the global product
    # names already treated as immutable entities by the reply contract; free-form entity
    # extraction does not belong in a schema migration.
    bind = op.get_bind()
    for term in _KNOWN_GLOBAL_BRAND_TERMS:
        bind.execute(
            sa.text(
                "UPDATE knowledge_documents "
                "SET protected_values = protected_values || jsonb_build_array(:term) "
                "WHERE strpos(reply, :term) > 0 "
                "AND NOT protected_values @> jsonb_build_array(:term)"
            ),
            {"term": term},
        )

    # protected_values changes the deterministic generation policy, so it must also change the
    # persisted revision identity. Existing decisions and reviewed localizations deliberately keep
    # their old hashes and become stale instead of being silently re-authorized under the new policy.
    _rewrite_knowledge_chunk_hashes(include_policy=True)


def downgrade() -> None:
    # The predecessor has only outbox_id. Restore a review-only link before dropping the
    # additive column, but fail closed when both links exist because that state cannot be
    # represented losslessly by the old schema.
    op.execute(
        sa.text(
            "DO $$ BEGIN "
            "IF EXISTS ("
            "SELECT 1 FROM reply_decisions "
            "WHERE review_outbox_id IS NOT NULL AND outbox_id IS NOT NULL"
            ") THEN "
            "RAISE EXCEPTION 'cannot downgrade reply review provenance with dual outbox links'; "
            "END IF; "
            "END $$"
        )
    )
    protected_count = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM knowledge_documents "
            "WHERE protected_values <> '[]'::jsonb"
        )
    ).scalar_one()
    if protected_count:
        raise RuntimeError(
            "cannot downgrade knowledge policy while protected values are present"
        )
    # Rows with no policy use the predecessor's legacy content-only identity. This also repairs a
    # policy hash after an operator has explicitly cleared protected values for a schema downgrade.
    _rewrite_knowledge_chunk_hashes(include_policy=False)
    op.execute(
        sa.text(
            "UPDATE reply_decisions "
            "SET outbox_id = review_outbox_id "
            "WHERE review_outbox_id IS NOT NULL"
        )
    )
    op.drop_constraint(
        "ck_knowledge_documents_protected_values_array",
        "knowledge_documents",
        type_="check",
    )
    op.drop_constraint(
        "ck_reply_decisions_rag_evidence_object",
        "reply_decisions",
        type_="check",
    )
    op.drop_column("knowledge_documents", "protected_values")
    op.drop_column("reply_decisions", "rag_evidence")
    op.drop_column("reply_decisions", "selector_version")
    op.drop_column("reply_decisions", "retrieval_policy_version")
    op.drop_column("reply_decisions", "decision_release_sha")
    op.drop_constraint(
        "fk_reply_decisions_review_outbox_id",
        "reply_decisions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_reply_decisions_review_outbox_id",
        "reply_decisions",
        type_="unique",
    )
    op.drop_column("reply_decisions", "review_outbox_id")
