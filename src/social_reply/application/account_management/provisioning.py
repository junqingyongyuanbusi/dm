import hashlib
import hmac
import json
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from social_reply.application.account_management.access import (
    lock_user_authority,
    require_reauthorization,
)
from social_reply.application.account_management.agent_control_plane import (
    ensure_agent_deployment_for_channel_scope,
)
from social_reply.application.account_management.auth import principal_from_session_row
from social_reply.connectors.email.contracts import normalize_email_address
from social_reply.domain.platform_accounts import (
    ACTIVE_ACCOUNT_STATUS,
    account_platform,
    canonical_account_status,
    normalize_account_capability,
)
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.shared.config import get_settings

_CONTROL_API_SECRET_KEY = "__control_api_trust__"
_CONTROL_API_SECRET_VALUE = "v1"
_CHECKPOINT_KEY = "checkpoint"
_CHECKPOINT_ACCOUNT_PERSISTED = "ACCOUNT_PERSISTED"
_CHECKPOINT_CREDENTIALS_APPLIED = "CREDENTIALS_APPLIED"
_CHECKPOINT_SUBSCRIPTIONS_APPLIED = "SUBSCRIPTIONS_APPLIED"


async def bind_provisioning_external_identity(
    *,
    provisioning_job_id: uuid.UUID | None,
    provisioning_attempt_count: int | None,
    platform: str,
    external_account_id: str,
    credential_bundle: dict[str, Any],
    platform_app_id: uuid.UUID | str | None,
) -> None:
    """Bind the provider-verified external identity to the locked job claim."""
    if provisioning_job_id is None or provisioning_attempt_count is None:
        raise ValueError("provisioning_claim_required")
    identity = external_account_id.strip()
    if not identity:
        raise ValueError("platform_account_target_identity_missing")
    async with get_session_factory()() as session:
        job = (
            await session.execute(
                select(models.ProvisioningJob)
                .where(
                    models.ProvisioningJob.id == provisioning_job_id,
                    models.ProvisioningJob.platform == platform,
                    models.ProvisioningJob.status == "PROCESSING",
                    models.ProvisioningJob.attempt_count == provisioning_attempt_count,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise ValueError("provisioning_claim_lost")
        request = dict(job.request or {})
        if platform == "feishu":
            requested_identity = request.get("app_id")
        elif platform == "email":
            requested_identity = request.get("email_address")
            if requested_identity is not None:
                requested_identity = normalize_email_address(str(requested_identity))
        else:
            requested_identity = request.get("external_account_id")
        if requested_identity is not None and str(requested_identity) != identity:
            raise ValueError("platform_account_target_mismatch")
        result = dict(job.result or {})
        bound_identity = result.get("bound_external_account_id")
        if bound_identity is not None and str(bound_identity) != identity:
            raise ValueError("provisioning_authority_invalid")
        bound_credentials = result.get("bound_credential_fingerprint")
        if bound_credentials is not None and not credential_fingerprint_matches(
            credential_bundle, bound_credentials
        ):
            raise ValueError("provisioning_authority_invalid")
        credential_digest = credential_fingerprint(credential_bundle)
        bound_app_id = result.get("bound_platform_app_id")
        normalized_app_id = str(platform_app_id) if platform_app_id is not None else None
        if "bound_platform_app_id" in result and bound_app_id != normalized_app_id:
            raise ValueError("provisioning_authority_invalid")
        job.result = {
            **result,
            "bound_external_account_id": identity,
            "bound_credential_fingerprint": credential_digest,
            "bound_platform_app_id": normalized_app_id,
        }
        await session.commit()


def credential_fingerprint_candidates(bundle: dict[str, Any]) -> tuple[str, ...]:
    """Return HMAC identities for every configured secret key during rotation."""
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    keys = get_settings().platform_secret_key_list
    if not keys:
        raise ValueError("platform_secret_keys_required")
    return tuple(
        hmac.new(key.encode("utf-8"), encoded.encode("utf-8"), hashlib.sha256).hexdigest()
        for key in keys
    )


PRIVATE_INPUT_FINGERPRINT_KEY = "__provisioning_input_fingerprint"
PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY = (
    "__provisioning_input_fingerprint_without_required"
)
_REQUIRED_SECRET_FIELDS = ("xchat_pin", "password")


def credential_fingerprint(bundle: dict[str, Any]) -> str:
    """Return the current primary non-secret identity for the credential payload."""
    return credential_fingerprint_candidates(bundle)[0]


def credential_fingerprint_matches(bundle: dict[str, Any], expected: object) -> bool:
    return isinstance(expected, str) and expected in credential_fingerprint_candidates(bundle)


def _input_fingerprint_payload(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
    omit_secret: str | None = None,
) -> str:
    del expected_config_version
    credentials = {
        str(key): str(value)
        for key, value in credential_bundle.items()
        if key != _CONTROL_API_SECRET_KEY and key != omit_secret
    }
    payload = {
        "version": 1,
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "platform": platform,
        "operation": operation,
        "target_account_id": (
            str(target_account_id) if target_account_id is not None else None
        ),
        # Config versions are checked separately; recovery may advance this value.
        "request": dict(request),
        "credentials": credentials,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    )


def provisioning_input_fingerprint_candidates(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
    omit_secret: str | None = None,
) -> tuple[str, ...]:
    encoded = _input_fingerprint_payload(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        request=request,
        credential_bundle=credential_bundle,
        omit_secret=omit_secret,
    ).encode("utf-8")
    keys = get_settings().platform_secret_key_list
    if not keys:
        raise ValueError("platform_secret_keys_required")
    return tuple(
        hmac.new(key.encode("utf-8"), encoded, hashlib.sha256).hexdigest() for key in keys
    )


def provisioning_input_fingerprint(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
    omit_secret: str | None = None,
) -> str:
    return provisioning_input_fingerprint_candidates(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        request=request,
        credential_bundle=credential_bundle,
        omit_secret=omit_secret,
    )[0]


def provisioning_input_fingerprint_matches(
    *,
    expected: object,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
    omit_secret: str | None = None,
) -> bool:
    return isinstance(expected, str) and expected in provisioning_input_fingerprint_candidates(
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        request=request,
        credential_bundle=credential_bundle,
        omit_secret=omit_secret,
    )


def provisioning_input_fingerprint_record(
    *,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
) -> dict[str, Any]:
    return {
        PRIVATE_INPUT_FINGERPRINT_KEY: provisioning_input_fingerprint(
            tenant_id=tenant_id,
            brand_id=brand_id,
            platform=platform,
            operation=operation,
            target_account_id=target_account_id,
            expected_config_version=expected_config_version,
            request=request,
            credential_bundle=credential_bundle,
        ),
        PRIVATE_INPUT_FINGERPRINT_WITHOUT_REQUIRED_KEY: {
            secret_name: provisioning_input_fingerprint(
                tenant_id=tenant_id,
                brand_id=brand_id,
                platform=platform,
                operation=operation,
                target_account_id=target_account_id,
                expected_config_version=expected_config_version,
                request=request,
                credential_bundle=credential_bundle,
                omit_secret=secret_name,
            )
            for secret_name in _REQUIRED_SECRET_FIELDS
        },
    }


def provisioning_input_fingerprint_without_required_matches(
    *,
    expected: object,
    required_secret: str,
    tenant_id: str,
    brand_id: str,
    platform: str,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    request: dict[str, Any],
    credential_bundle: dict[str, Any],
) -> bool:
    return provisioning_input_fingerprint_matches(
        expected=expected,
        tenant_id=tenant_id,
        brand_id=brand_id,
        platform=platform,
        operation=operation,
        target_account_id=target_account_id,
        expected_config_version=expected_config_version,
        request=request,
        credential_bundle=credential_bundle,
        omit_secret=required_secret,
    )


def provisioning_checkpoint(job: models.ProvisioningJob) -> dict[str, Any] | None:
    result = getattr(job, "result", None)
    checkpoint = result.get(_CHECKPOINT_KEY) if isinstance(result, dict) else None
    return dict(checkpoint) if isinstance(checkpoint, dict) else None


def checkpoint_output_version(checkpoint: dict[str, Any]) -> int:
    try:
        value = int(checkpoint["output_config_version"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("provisioning_checkpoint_invalid") from exc
    if value < 1:
        raise ValueError("provisioning_checkpoint_invalid")
    return value


def checkpoint_matches_job(
    job: models.ProvisioningJob,
    checkpoint: dict[str, Any],
    *,
    account_id: uuid.UUID | str | None = None,
    external_account_id: str | None = None,
) -> bool:
    """Check the durable identity before accepting any checkpoint recovery."""
    expected_target = (
        str(job.target_account_id) if getattr(job, "target_account_id", None) else None
    )
    checkpoint_target = checkpoint.get("target_account_id")
    if checkpoint.get("job_id") != str(job.id):
        return False
    if checkpoint.get("operation") != getattr(job, "operation", "CONNECT_ACCOUNT"):
        return False
    if checkpoint_target != expected_target:
        return False
    job_account_id = getattr(job, "account_id", None)
    checkpoint_account_id = checkpoint.get("account_id")
    if job_account_id is None or checkpoint_account_id != str(job_account_id):
        return False
    if account_id is not None and checkpoint_account_id != str(account_id):
        return False
    if (
        external_account_id is not None
        and checkpoint.get("external_account_id") != external_account_id
    ):
        return False
    return True


def _account_checkpoint(
    *,
    job: models.ProvisioningJob,
    account: models.PlatformAccount,
    phase: str,
    input_config_version: int | None,
    output_config_version: int,
    credential_bundle: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "job_id": str(job.id),
        "operation": job.operation,
        "target_account_id": (
            str(job.target_account_id) if job.target_account_id is not None else None
        ),
        "account_id": str(account.id),
        "external_account_id": account.external_account_id,
        "public_id": account.public_id,
        "input_config_version": input_config_version,
        "output_config_version": output_config_version,
        "credential_fingerprint": credential_fingerprint(credential_bundle),
        "phase": phase,
        **extra,
    }


def _reauthorization_next_phase(platform: str) -> str:
    if platform in {"facebook", "instagram", "whatsapp"}:
        return "SUBSCRIPTIONS_PENDING"
    if platform == "email":
        return "MANUAL_RECONNECT_REVIEW"
    return "WEBHOOK_REVIEW_PENDING"


def make_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12).replace('-', '_')}"


def tenant_public_id(prefix: str, tenant_id: str) -> str:
    safe_tenant = "".join(
        character
        if character.isascii() and (character.isalnum() or character in {"_", "-"})
        else "_"
        for character in tenant_id
    ).strip("_")
    safe_tenant = safe_tenant or "tenant"
    if safe_tenant != tenant_id:
        digest = hashlib.sha256(tenant_id.encode()).hexdigest()[:8]
        safe_tenant = f"{safe_tenant}_{digest}"
    return f"{prefix}_{safe_tenant}"


async def provision_platform_app(
    *,
    platform_family: str,
    external_app_id: str | None,
    tenant_id: str,
    name: str,
    public_id: str | None,
    public_id_prefix: str,
    secrets_root: Path,
    credential_bundle: dict[str, str],
    config: dict,
    allow_external_app_id_rotation: bool = False,
) -> tuple[uuid.UUID, str]:
    """Create or reuse one shared App without implicit credential rotation."""
    del secrets_root, allow_external_app_id_rotation
    external_app_id = external_app_id.strip() if external_app_id else None
    public_id = public_id.strip() if public_id else None
    if not external_app_id and not public_id:
        raise ValueError("platform_app_identity_required")

    lock_identity = (
        "|".join(sorted(value for value in (public_id, external_app_id) if value))
        or platform_family
    )
    async with get_session_factory()() as session:
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"platform-app:{tenant_id}:{platform_family}:{lock_identity}"},
        )
        existing_by_external = None
        if external_app_id:
            existing_by_external = (
                await session.execute(
                    select(models.PlatformApp)
                    .where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.external_app_id == external_app_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
        existing_by_public = None
        if public_id:
            existing_by_public = (
                await session.execute(
                    select(models.PlatformApp)
                    .where(
                        models.PlatformApp.tenant_id == tenant_id,
                        models.PlatformApp.platform_family == platform_family,
                        models.PlatformApp.public_id == public_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
        if (
            existing_by_external is not None
            and existing_by_public is not None
            and existing_by_external.id != existing_by_public.id
        ):
            raise ValueError("platform_app_external_id_already_bound")
        existing = existing_by_external or existing_by_public

        if public_id and platform_family in {"meta", "instagram"}:
            shared_route_owner = (
                await session.execute(
                    select(models.PlatformApp)
                    .where(
                        models.PlatformApp.platform_family.in_(("meta", "instagram")),
                        models.PlatformApp.public_id == public_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                shared_route_owner is not None
                and shared_route_owner.platform_family != platform_family
            ):
                raise ValueError("meta_webhook_public_id_collision")

        if existing is not None:
            if external_app_id and existing.external_app_id != external_app_id:
                raise ValueError("platform_app_public_id_external_id_mismatch")
            if public_id and existing.public_id != public_id:
                raise ValueError("platform_app_public_id_is_immutable")
            stored_credentials = (
                decrypt_secret_bundle(existing.credential_bundle)
                if existing.credential_bundle
                else {}
            )
            if stored_credentials != credential_bundle:
                raise ValueError("platform_app_rotation_required")
            existing.name = name
            existing.credential_bundle = encrypt_secret_bundle(credential_bundle)
            existing.config = config
            existing.config_version += 1
            await session.commit()
            return existing.id, existing.public_id

        resolved_public_id = public_id or make_public_id(public_id_prefix)
        row = models.PlatformApp(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            platform_family=platform_family,
            name=name,
            external_app_id=external_app_id,
            public_id=resolved_public_id,
            credential_bundle=encrypt_secret_bundle(credential_bundle),
            config=config,
            config_version=1,
            status=ACTIVE_ACCOUNT_STATUS,
        )
        session.add(row)
        await session.flush()
        await session.commit()
        return row.id, row.public_id


async def _assert_provisioning_claim(
    session,
    *,
    provisioning_job_id: uuid.UUID | None,
    provisioning_attempt_count: int | None,
) -> models.ProvisioningJob | None:
    if (provisioning_job_id is None) != (provisioning_attempt_count is None):
        raise ValueError("provisioning_claim_fence_incomplete")
    if provisioning_job_id is None:
        return None
    claim = (
        await session.execute(
            select(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.id == provisioning_job_id,
                models.ProvisioningJob.status == "PROCESSING",
                models.ProvisioningJob.attempt_count == provisioning_attempt_count,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if claim is None:
        raise ValueError("provisioning_claim_lost")
    if (
        claim.authority_kind not in {"STAFF_SESSION", "BOOTSTRAP_SESSION", "CONTROL_API"}
        or claim.authority_version != 1
    ):
        raise PermissionError("provisioning_authority_invalid")
    return claim


async def _validate_persist_authority(
    session,
    *,
    tenant_id: str,
    brand_id: str,
    owner_user_id: uuid.UUID | None,
    platform: str,
    external_account_id: str,
    credential_bundle: dict[str, Any],
    platform_app_id: uuid.UUID | str | None,
    operation: str,
    target_account_id: uuid.UUID | str | None,
    expected_config_version: int | None,
    initiator_user_id: uuid.UUID | None,
    initiator_session_id: uuid.UUID | str | None,
    authority_kind: str,
    authority_version: int,
    trusted_control_api: bool,
    provisioning_job_id: uuid.UUID | None,
    provisioning_attempt_count: int | None,
):
    if authority_version != 1 or authority_kind not in {
        "STAFF_SESSION",
        "BOOTSTRAP_SESSION",
        "CONTROL_API",
    }:
        raise PermissionError("provisioning_authority_invalid")
    if provisioning_job_id is None or provisioning_attempt_count is None:
        raise PermissionError("provisioning_claim_required")

    principal = None
    if authority_kind != "CONTROL_API":
        if trusted_control_api or initiator_session_id is None:
            raise PermissionError("provisioning_authority_invalid")
        if authority_kind == "STAFF_SESSION" and initiator_user_id is None:
            raise PermissionError("initiator_session_invalid")
        if authority_kind == "BOOTSTRAP_SESSION" and initiator_user_id is not None:
            raise PermissionError("initiator_session_invalid")
        initial_principal = await principal_from_session_row(
            session, initiator_session_id, for_update=False
        )
        if (
            initial_principal is None
            or initial_principal.user_id != initiator_user_id
            or tenant_id not in initial_principal.allowed_tenants
        ):
            raise PermissionError("initiator_session_invalid")
        if initiator_user_id is not None:
            await lock_user_authority(session, initiator_user_id)
        principal = await principal_from_session_row(session, initiator_session_id, for_update=True)
        if (
            principal is None
            or principal.session_id != uuid.UUID(str(initiator_session_id))
            or principal.user_id != initiator_user_id
            or tenant_id not in principal.allowed_tenants
            or principal.must_change_password
            or (authority_kind == "BOOTSTRAP_SESSION" and not principal.is_superadmin)
            or (
                authority_kind == "STAFF_SESSION"
                and (principal.user_id is None or principal.role not in {"USER", "WORKSPACE_ADMIN"})
            )
        ):
            raise PermissionError("initiator_session_invalid")

    claim = (
        await session.execute(
            select(models.ProvisioningJob)
            .where(
                models.ProvisioningJob.id == provisioning_job_id,
                models.ProvisioningJob.attempt_count == provisioning_attempt_count,
                models.ProvisioningJob.status == "PROCESSING",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if claim is None:
        raise ValueError("provisioning_claim_lost")
    expected_session_id = (
        uuid.UUID(str(initiator_session_id)) if initiator_session_id is not None else None
    )
    if (
        claim.tenant_id != tenant_id
        or (claim.result or {}).get("bound_external_account_id") != external_account_id
        or not credential_fingerprint_matches(
            credential_bundle,
            (claim.result or {}).get("bound_credential_fingerprint"),
        )
        or (claim.result or {}).get("bound_platform_app_id")
        != (str(platform_app_id) if platform_app_id is not None else None)
        or claim.brand_id != brand_id
        or claim.owner_user_id != owner_user_id
        or claim.platform != platform
        or claim.operation != operation
        or claim.target_account_id
        != (uuid.UUID(str(target_account_id)) if target_account_id is not None else None)
        or claim.initiator_user_id != initiator_user_id
        or claim.initiator_session_id != expected_session_id
        or claim.authority_kind != authority_kind
        or claim.authority_version != authority_version
        or (
            operation == "REAUTHORIZE"
            and claim.expected_config_version != expected_config_version
            and not (
                (checkpoint := provisioning_checkpoint(claim)) is not None
                and checkpoint.get("phase") == _CHECKPOINT_CREDENTIALS_APPLIED
                and checkpoint_matches_job(claim, checkpoint)
            )
        )
    ):
        raise PermissionError("provisioning_authority_invalid")
    try:
        marker_values = decrypt_secret_bundle(claim.staging_secret) if claim.staging_secret else {}
    except (TypeError, ValueError) as exc:
        raise PermissionError("provisioning_authority_invalid") from exc
    claim_is_control = claim.authority_kind == "CONTROL_API"
    if (
        claim_is_control != (authority_kind == "CONTROL_API")
        or claim_is_control != trusted_control_api
        or marker_values.get(_CONTROL_API_SECRET_KEY)
        != (_CONTROL_API_SECRET_VALUE if claim_is_control else None)
    ):
        raise PermissionError("provisioning_authority_invalid")
    if claim_is_control and (initiator_user_id is not None or initiator_session_id is not None):
        raise PermissionError("provisioning_authority_invalid")
    return principal


async def _reauthorize_direct_account(
    *,
    platform: str,
    external_account_id: str,
    tenant_id: str,
    brand_id: str,
    owner_user_id: uuid.UUID | None,
    credential_bundle: dict[str, str],
    webhook_secret_bundle: dict[str, str] | None,
    provider_username: str | None,
    avatar_url: str | None,
    profile_updated_at: datetime | None,
    target_account_id: uuid.UUID | str,
    platform_app_id: uuid.UUID | None,
    expected_config_version: int,
    initiator_user_id: uuid.UUID | None,
    initiator_session_id: uuid.UUID | str | None,
    authority_kind: str,
    authority_version: int,
    trusted_control_api: bool,
    provisioning_job_id: uuid.UUID | None,
    provisioning_attempt_count: int | None,
    preserve_existing_webhook_secret: bool,
    derived_config_patch: dict | None = None,
) -> tuple[uuid.UUID, str]:
    platform = account_platform(platform).value
    encrypted_credentials = encrypt_secret_bundle(credential_bundle)
    async with get_session_factory()() as session:
        principal = await _validate_persist_authority(
            session,
            tenant_id=tenant_id,
            brand_id=brand_id,
            owner_user_id=owner_user_id,
            platform=platform,
            external_account_id=external_account_id,
            credential_bundle=credential_bundle,
            platform_app_id=platform_app_id,
            operation="REAUTHORIZE",
            target_account_id=target_account_id,
            expected_config_version=expected_config_version,
            initiator_user_id=initiator_user_id,
            initiator_session_id=initiator_session_id,
            authority_kind=authority_kind,
            authority_version=authority_version,
            trusted_control_api=trusted_control_api,
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
        )
        claim = await _assert_provisioning_claim(
            session,
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
        )
        try:
            account_uuid = uuid.UUID(str(target_account_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("platform_account_target_invalid") from exc
        account = (
            await session.execute(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.id == account_uuid,
                    models.PlatformAccount.tenant_id == tenant_id,
                    models.PlatformAccount.platform == platform,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if account is None:
            raise LookupError("platform_account_target_not_found")
        if account.external_account_id != external_account_id:
            raise ValueError("platform_account_target_mismatch")
        checkpoint = provisioning_checkpoint(claim) if claim is not None else None
        if checkpoint is not None:
            if checkpoint.get(
                "phase"
            ) != _CHECKPOINT_CREDENTIALS_APPLIED or not checkpoint_matches_job(
                claim,
                checkpoint,
                account_id=account.id,
                external_account_id=external_account_id,
            ):
                raise PermissionError("provisioning_authority_invalid")
            output_version = checkpoint_output_version(checkpoint)
            if account.config_version < output_version:
                raise ValueError("provisioning_checkpoint_lost")
            if principal is not None:
                await require_reauthorization(
                    session,
                    principal=principal,
                    account=account,
                    expected_config_version=None,
                )
            try:
                current_credentials = decrypt_secret_bundle(account.credential_bundle)
            except (TypeError, ValueError) as exc:
                raise ValueError("provisioning_checkpoint_invalid") from exc
            checkpoint = {
                **checkpoint,
                "observed_config_version": account.config_version,
                "applied_credentials_current": credential_fingerprint_matches(
                    current_credentials,
                    checkpoint.get("credential_fingerprint"),
                ),
                "superseded_by_later_config": account.config_version > output_version,
            }
            claim.result = {**dict(claim.result or {}), _CHECKPOINT_KEY: checkpoint}
            claim.account_id = account.id
            claim.current_step = _CHECKPOINT_CREDENTIALS_APPLIED
            await session.commit()
            if account.public_id is None:
                raise RuntimeError("platform_account_public_id_missing")
            return account.id, account.public_id
        if account.config_version != expected_config_version:
            raise ValueError("account_reauthorization_version_conflict")
        if principal is not None:
            await require_reauthorization(
                session,
                principal=principal,
                account=account,
                expected_config_version=expected_config_version,
            )
        update_values = {
            "credential_bundle": encrypted_credentials,
            "config_version": models.PlatformAccount.config_version + 1,
        }
        if webhook_secret_bundle is not None and not (
            preserve_existing_webhook_secret and account.webhook_secret_bundle
        ):
            update_values["webhook_secret_bundle"] = encrypt_secret_bundle(webhook_secret_bundle)
        if provider_username is not None:
            update_values["provider_username"] = provider_username
        if avatar_url is not None:
            update_values["avatar_url"] = avatar_url
        if profile_updated_at is not None:
            update_values["profile_updated_at"] = profile_updated_at
        if derived_config_patch:
            update_values["config"] = models.PlatformAccount.config.op("||")(derived_config_patch)
        persisted = (
            await session.execute(
                models.PlatformAccount.__table__.update()
                .where(
                    models.PlatformAccount.id == account.id,
                    models.PlatformAccount.tenant_id == tenant_id,
                    models.PlatformAccount.platform == platform,
                    models.PlatformAccount.external_account_id == external_account_id,
                    models.PlatformAccount.config_version == expected_config_version,
                )
                .values(**update_values)
                .returning(models.PlatformAccount.id, models.PlatformAccount.public_id)
            )
        ).one_or_none()
        if persisted is None:
            raise ValueError("account_reauthorization_version_conflict")
        if claim is None:
            raise ValueError("provisioning_claim_required")
        checkpoint = _account_checkpoint(
            job=claim,
            account=account,
            phase=_CHECKPOINT_CREDENTIALS_APPLIED,
            input_config_version=expected_config_version,
            output_config_version=expected_config_version + 1,
            credential_bundle=credential_bundle,
            next_phase=_reauthorization_next_phase(platform),
            preserve_disabled_intent=(
                account.status != ACTIVE_ACCOUNT_STATUS
                and not bool((account.config or {}).get("meta_disabled_by_provisioning"))
            ),
        )
        claim.account_id = account.id
        claim.current_step = _CHECKPOINT_CREDENTIALS_APPLIED
        claim.result = {**dict(claim.result or {}), _CHECKPOINT_KEY: checkpoint}
        await session.commit()
    if persisted.public_id is None:
        raise RuntimeError("platform_account_public_id_missing")
    return persisted.id, persisted.public_id


async def provision_direct_account(
    *,
    platform: str,
    external_account_id: str,
    tenant_id: str,
    brand_id: str,
    name: str,
    public_id: str | None,
    public_id_prefix: str,
    secrets_root: Path,
    credential_bundle: dict[str, str],
    webhook_secret_bundle: dict[str, str] | None,
    config: dict,
    capability: dict,
    automation_default: str,
    owner_user_id: uuid.UUID | None = None,
    provider_username: str | None = None,
    avatar_url: str | None = None,
    profile_updated_at: datetime | None = None,
    platform_app_id: uuid.UUID | None = None,
    preserve_existing_webhook_secret: bool = False,
    status: str | None = None,
    derived_config_patch: dict | None = None,
    trusted_control_api: bool = False,
    authority_kind: str = "UNVERIFIED",
    authority_version: int = 0,
    operation: str = "CONNECT_ACCOUNT",
    target_account_id: uuid.UUID | str | None = None,
    expected_config_version: int | None = None,
    initiator_user_id: uuid.UUID | None = None,
    initiator_session_id: uuid.UUID | str | None = None,
    provisioning_job_id: uuid.UUID | None = None,
    provisioning_attempt_count: int | None = None,
) -> tuple[uuid.UUID, str]:
    """Create a new account or update credentials through an explicit operation path."""
    del secrets_root
    platform = account_platform(platform).value
    if operation == "REAUTHORIZE":
        if target_account_id is None or expected_config_version is None:
            raise ValueError("platform_account_target_required")
        return await _reauthorize_direct_account(
            platform=platform,
            external_account_id=external_account_id,
            tenant_id=tenant_id,
            brand_id=brand_id,
            owner_user_id=owner_user_id,
            credential_bundle=credential_bundle,
            webhook_secret_bundle=webhook_secret_bundle,
            provider_username=provider_username,
            avatar_url=avatar_url,
            profile_updated_at=profile_updated_at,
            target_account_id=target_account_id,
            platform_app_id=platform_app_id,
            expected_config_version=expected_config_version,
            initiator_user_id=initiator_user_id,
            initiator_session_id=initiator_session_id,
            authority_kind=authority_kind,
            authority_version=authority_version,
            trusted_control_api=trusted_control_api,
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
            preserve_existing_webhook_secret=preserve_existing_webhook_secret,
            derived_config_patch=derived_config_patch,
        )
    if operation != "CONNECT_ACCOUNT":
        raise ValueError("unsupported_provisioning_operation")
    if target_account_id is not None or expected_config_version is not None:
        raise ValueError("connect_target_not_allowed")
    capability = normalize_account_capability(platform, capability)
    if authority_kind == "CONTROL_API" and operation == "REAUTHORIZE":
        raise PermissionError("reauthorization_requires_session")
    async with get_session_factory()() as session:
        await _validate_persist_authority(
            session,
            tenant_id=tenant_id,
            brand_id=brand_id,
            owner_user_id=owner_user_id,
            platform=platform,
            external_account_id=external_account_id,
            credential_bundle=credential_bundle,
            platform_app_id=platform_app_id,
            operation="CONNECT_ACCOUNT",
            target_account_id=None,
            expected_config_version=None,
            initiator_user_id=initiator_user_id,
            initiator_session_id=initiator_session_id,
            authority_kind=authority_kind,
            authority_version=authority_version,
            trusted_control_api=trusted_control_api,
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
        )
        claim = await _assert_provisioning_claim(
            session,
            provisioning_job_id=provisioning_job_id,
            provisioning_attempt_count=provisioning_attempt_count,
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"platform-account:{tenant_id}:{platform}:{external_account_id}"},
        )
        locked_existing = (
            await session.execute(
                select(models.PlatformAccount)
                .where(
                    models.PlatformAccount.tenant_id == tenant_id,
                    models.PlatformAccount.platform == platform,
                    models.PlatformAccount.external_account_id == external_account_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()

        if locked_existing is not None:
            checkpoint = provisioning_checkpoint(claim) if claim is not None else None
            if (
                claim is None
                or checkpoint is None
                or checkpoint.get("phase")
                not in {_CHECKPOINT_ACCOUNT_PERSISTED, _CHECKPOINT_SUBSCRIPTIONS_APPLIED}
                or not checkpoint_matches_job(
                    claim,
                    checkpoint,
                    account_id=locked_existing.id,
                    external_account_id=external_account_id,
                )
            ):
                raise ValueError("platform_account_already_exists")
            checkpoint_version = checkpoint_output_version(checkpoint)
            if claim.expected_config_version != checkpoint_version:
                raise ValueError("provisioning_checkpoint_invalid")
            if locked_existing.config_version < checkpoint_version:
                raise ValueError("provisioning_checkpoint_lost")
            if (
                checkpoint.get("phase") == _CHECKPOINT_ACCOUNT_PERSISTED
                and locked_existing.config_version > checkpoint_version
            ):
                checkpoint = {
                    **checkpoint,
                    "superseded_by_later_config": True,
                    "observed_config_version": locked_existing.config_version,
                }
            claim.result = {**dict(claim.result or {}), _CHECKPOINT_KEY: checkpoint}
            claim.current_step = checkpoint.get("phase") or _CHECKPOINT_ACCOUNT_PERSISTED
            if checkpoint.get("phase") == _CHECKPOINT_SUBSCRIPTIONS_APPLIED:
                claim.platform_app_id = (
                    getattr(claim, "platform_app_id", None) or locked_existing.platform_app_id
                )
            if public_id is not None and locked_existing.public_id != public_id:
                raise ValueError("platform_account_public_id_is_immutable")
            await session.commit()
            if not locked_existing.public_id:
                raise RuntimeError("platform_account_public_id_missing")
            return locked_existing.id, locked_existing.public_id

        if automation_default != "BOT_DRAFT_ONLY":
            raise ValueError("new_account_requires_bot_draft_only")
        resolved_public_id = public_id or make_public_id(public_id_prefix)
        webhook_bundle = (
            encrypt_secret_bundle(webhook_secret_bundle)
            if webhook_secret_bundle is not None
            else None
        )
        account_id = uuid.uuid4()
        values = {
            "id": account_id,
            "tenant_id": tenant_id,
            "brand_id": brand_id,
            "platform": platform,
            "platform_app_id": platform_app_id,
            "owner_user_id": owner_user_id,
            "name": name,
            "provider_username": provider_username,
            "avatar_url": avatar_url,
            "profile_updated_at": profile_updated_at,
            "external_account_id": external_account_id,
            "public_id": resolved_public_id,
            "credential_bundle": encrypt_secret_bundle(credential_bundle),
            "webhook_secret_bundle": webhook_bundle,
            "config": {"delivery_mode": "direct", **config},
            "capability": capability,
            "config_version": 1,
            "automation_default": automation_default,
            "status": (
                canonical_account_status(status) if status is not None else ACTIVE_ACCOUNT_STATUS
            ),
        }
        persisted = (
            await session.execute(
                pg_insert(models.PlatformAccount)
                .values(**values)
                .on_conflict_do_nothing(
                    index_elements=["tenant_id", "platform", "external_account_id"]
                )
                .returning(models.PlatformAccount.id, models.PlatformAccount.public_id)
            )
        ).one_or_none()
        if persisted is None:
            raise ValueError("platform_account_already_exists")

        await ensure_agent_deployment_for_channel_scope(
            session,
            tenant_id=tenant_id,
            brand_id=brand_id,
        )
        if claim is not None:
            persisted_account = await session.get(models.PlatformAccount, persisted.id)
            if persisted_account is None:
                raise ValueError("provisioning_claim_lost")
            checkpoint = _account_checkpoint(
                job=claim,
                account=persisted_account,
                phase=_CHECKPOINT_ACCOUNT_PERSISTED,
                input_config_version=None,
                output_config_version=1,
                credential_bundle=credential_bundle,
            )
            claim.account_id = persisted.id
            claim.expected_config_version = 1
            claim.current_step = _CHECKPOINT_ACCOUNT_PERSISTED
            claim.result = {**dict(claim.result or {}), _CHECKPOINT_KEY: checkpoint}
        await session.commit()
    if persisted.public_id is None:
        raise RuntimeError("platform_account_public_id_missing")
    return persisted.id, persisted.public_id
