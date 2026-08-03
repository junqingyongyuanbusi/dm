import logging
import time
import uuid
from datetime import UTC, datetime

import httpx

from social_reply.application.platform_accounts import (
    PlatformAccountRuntime,
    list_active_accounts_by_platform,
)
from social_reply.connectors.feishu.client import FeishuClient, FeishuClientError
from social_reply.infrastructure.database import models
from social_reply.infrastructure.database.engine import get_session_factory
from social_reply.shared.config import get_settings

logger = logging.getLogger(__name__)
_last_check_at: float | None = None
_CREDENTIAL_ERROR_CODES = frozenset({"FEISHU_API_10003"})


def _health_error_code(exc: Exception) -> str:
    if isinstance(exc, FeishuClientError):
        return exc.code
    if isinstance(exc, httpx.ConnectTimeout):
        return "FEISHU_CONNECT_TIMEOUT"
    if isinstance(exc, httpx.TimeoutException):
        return "FEISHU_TIMEOUT"
    if isinstance(exc, httpx.TransportError):
        return "FEISHU_TRANSPORT_ERROR"
    if isinstance(exc, (KeyError, ValueError)):
        return "FEISHU_CREDENTIAL_INVALID"
    return f"FEISHU_{exc.__class__.__name__.upper()}"


async def _save_health(
    account_id: uuid.UUID,
    *,
    status: str,
    error_code: str | None = None,
    bot_open_id: str | None = None,
    bot_name: str | None = None,
    bot_activate_status: int | None = None,
) -> None:
    health = {
        "feishu_health_status": status,
        "feishu_health_checked_at": datetime.now(UTC).isoformat(),
        "feishu_health_error_code": error_code,
    }
    if bot_open_id is not None:
        health["feishu_bot_open_id"] = bot_open_id
    if bot_name is not None:
        health["feishu_bot_name"] = bot_name
    if bot_activate_status is not None:
        health["feishu_bot_activate_status"] = bot_activate_status
    async with get_session_factory()() as session:
        await session.execute(
            models.PlatformAccount.__table__.update()
            .where(models.PlatformAccount.id == account_id)
            .values(config=models.PlatformAccount.config.op("||")(health))
        )
        await session.commit()


async def _check_account(account: PlatformAccountRuntime) -> str | None:
    try:
        credentials = account.credential_bundle
        app_id = credentials["app_id"].strip()
        app_secret = credentials["app_secret"].strip()
    except Exception as exc:  # noqa: BLE001 - encrypted credential failures are account-local
        error_code = _health_error_code(exc)
        await _save_health(account.id, status="CREDENTIAL_INVALID", error_code=error_code)
        return str(account.id)
    if not app_id or not app_secret or account.external_account_id != app_id:
        await _save_health(
            account.id,
            status="CREDENTIAL_INVALID",
            error_code="FEISHU_APP_ID_SCOPE_MISMATCH",
        )
        return str(account.id)

    client = FeishuClient(app_id=app_id, app_secret=app_secret)
    try:
        token, _expire = await client.tenant_access_token()
        bot = await client.get_bot_info(token, require_active=False)
        stored_open_id = str(account.config.get("feishu_bot_open_id") or "")
        if bot.activate_status != 2:
            status = "BOT_NOT_ACTIVE"
            error_code = "FEISHU_BOT_NOT_ACTIVATED"
        elif not stored_open_id or bot.open_id != stored_open_id:
            status = "BOT_ID_MISMATCH"
            error_code = "FEISHU_BOT_ID_MISMATCH"
        else:
            status = "READY"
            error_code = None
        await _save_health(
            account.id,
            status=status,
            error_code=error_code,
            bot_name=bot.name,
            bot_activate_status=bot.activate_status,
        )
        return str(account.id) if status != "READY" else None
    except Exception as exc:  # noqa: BLE001 - provider failures become sanitized health state
        error_code = _health_error_code(exc)
        status = "CREDENTIAL_INVALID" if error_code in _CREDENTIAL_ERROR_CODES else "ERROR"
        await _save_health(account.id, status=status, error_code=error_code)
        logger.warning(
            "feishu health check failed account=%s code=%s",
            account.id,
            error_code,
        )
        return str(account.id)
    finally:
        await client.aclose()


async def reconcile_feishu_account_health(*, force: bool = False) -> list[str]:
    global _last_check_at
    settings = get_settings()
    now = time.monotonic()
    if (
        not force
        and _last_check_at is not None
        and now - _last_check_at < settings.feishu_health_check_interval_seconds
    ):
        return []
    _last_check_at = now
    unhealthy: list[str] = []
    for account in await list_active_accounts_by_platform("feishu"):
        try:
            result = await _check_account(account)
        except Exception as exc:  # noqa: BLE001 - isolate accounts in the scheduler sweep
            error_code = _health_error_code(exc)
            await _save_health(account.id, status="ERROR", error_code=error_code)
            logger.exception(
                "feishu health account check crashed account=%s code=%s",
                account.id,
                error_code,
            )
            result = str(account.id)
        if result is not None:
            unhealthy.append(result)
    return unhealthy
