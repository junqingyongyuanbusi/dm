"""Validate Railway role assignment and shared production configuration without printing values."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping

from social_reply.shared.config import Settings

_SERVICES = ("api", "worker", "scheduler")
_EXPECTED_ROLES = {"api": "api", "worker": "worker", "scheduler": "scheduler"}
_RAILWAY_COMMAND_TIMEOUT_SECONDS = 60
_REQUIRED_SHARED = (
    "DATABASE_URL",
    "REDIS_URL",
    "PLATFORM_SECRET_KEYS",
    "CONTROL_API_KEY",
    "ADMIN_SESSION_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_ALLOWED_TENANTS",
    "PUBLIC_BASE_URL",
    "LLM_PROVIDER",
    "OPENAI_API_KEY",
    "TESTING",
)
_REQUIRED_EXPLICIT_GATES = (
    "CHATWOOT_ENABLED",
    "X_LEGACY_DM_ENABLED",
    "X_ACTIVITY_ENABLED",
    "XCHAT_ENABLED",
    "X_PUBLIC_REPLY_ENABLED",
    "FACEBOOK_MESSENGER_ENABLED",
    "INSTAGRAM_MESSAGING_ENABLED",
    "WHATSAPP_ENABLED",
    "FEISHU_ENABLED",
    "EMAIL_ENABLED",
    "EMAIL_AUTO_REPLY_ENABLED",
    "FEISHU_HANDOFF_NOTIFICATIONS_ENABLED",
    "META_AUTO_REPLY_ENABLED",
    "META_COMMENT_REPLY_ENABLED",
    "KNOWLEDGE_RETRIEVAL_ENABLED",
    "KNOWLEDGE_VERBATIM_REPLY",
    "REQUIRE_KNOWLEDGE",
    "MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED",
    "MULTILINGUAL_KNOWLEDGE_SHADOW_ENABLED",
    "ENGLISH_KNOWLEDGE_ONLY_ENABLED",
    "KNOWLEDGE_CORPUS_VERSION",
    "MULTILINGUAL_CALIBRATION_REPORT_SHA256",
    "MULTILINGUAL_SUPPORTED_LANGUAGES",
    "MULTILINGUAL_E2E_REPORT_SHA256",
    "KNOWLEDGE_AUTO_REPLY_MIN_SIMILARITY",
    "KNOWLEDGE_AUTO_REPLY_MIN_MARGIN",
    "OPENAI_GROUNDING_MODEL",
    "GROUNDING_VERIFIER_TIMEOUT_SECONDS",
)
_SHARED_SETTING_KEYS = tuple(name.upper() for name in Settings.model_fields)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _validated_settings(service: str, values: Mapping[str, str]) -> Settings:
    kwargs = {}
    for name, field in Settings.model_fields.items():
        key = name.upper()
        kwargs[name] = (
            values[key] if key in values else field.get_default(call_default_factory=True)
        )
    try:
        return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]
    except Exception as exc:
        raise ValueError(f"{service}:invalid_settings") from exc


def validate(variables: Mapping[str, Mapping[str, str]], *, public_base_url: str) -> None:
    errors: list[str] = []

    for service in _SERVICES:
        values = variables[service]
        expected_role = _EXPECTED_ROLES[service]
        if values.get("SERVICE_ROLE") != expected_role:
            errors.append(f"{service}:SERVICE_ROLE_must_equal_{expected_role}")
        if values.get("MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED", "").strip().lower() != "false":
            errors.append(f"{service}:MULTILINGUAL_KNOWLEDGE_REPLY_ENABLED_must_equal_false")
        if values.get("MULTILINGUAL_KNOWLEDGE_SHADOW_ENABLED", "").strip().lower() != "false":
            errors.append(f"{service}:MULTILINGUAL_KNOWLEDGE_SHADOW_ENABLED_must_equal_false")
        if values.get("ENGLISH_KNOWLEDGE_ONLY_ENABLED", "").strip().lower() != "false":
            errors.append(f"{service}:ENGLISH_KNOWLEDGE_ONLY_ENABLED_must_equal_false")
        if values.get("TESTING", "").strip().lower() != "false":
            errors.append(f"{service}:TESTING_must_equal_false")
        if values.get("PUBLIC_BASE_URL", "").rstrip("/") != public_base_url.rstrip("/"):
            errors.append(f"{service}:PUBLIC_BASE_URL_mismatch")
        for key in _REQUIRED_SHARED:
            if not values.get(key):
                errors.append(f"{service}:missing_{key}")
        for key in _REQUIRED_EXPLICIT_GATES:
            if key not in values:
                errors.append(f"{service}:missing_{key}")
        try:
            _validated_settings(service, values)
        except ValueError as exc:
            errors.append(str(exc))

    for key in _SHARED_SETTING_KEYS:
        present = [service for service in _SERVICES if key in variables[service]]
        if present and len(present) != len(_SERVICES):
            errors.append(f"shared_variable_partial:{key}")
            continue
        if len(present) == len(_SERVICES):
            fingerprints = {_fingerprint(variables[service][key]) for service in _SERVICES}
            if len(fingerprints) != 1:
                errors.append(f"shared_variable_mismatch:{key}")

    if errors:
        raise ValueError("railway_configuration_invalid:" + ",".join(errors))


def _railway_variables(project: str, environment: str, service: str) -> dict[str, str]:
    try:
        result = subprocess.run(
            [
                "railway",
                "variable",
                "list",
                "--project",
                project,
                "--environment",
                environment,
                "--service",
                service,
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=_RAILWAY_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"railway_variables_timeout:{service}") from exc
    if result.returncode != 0:
        raise ValueError(f"railway_variables_unavailable:{service}")
    value = json.loads(result.stdout)
    if not isinstance(value, Mapping):
        raise ValueError(f"railway_variables_must_be_object:{service}")
    return {str(key): str(item) for key, item in value.items()}


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(
            "usage: validate_railway_config.py <project_id> <environment> <public_base_url>"
        )
    project, environment, public_base_url = sys.argv[1:]
    try:
        validate(
            {service: _railway_variables(project, environment, service) for service in _SERVICES},
            public_base_url=public_base_url,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
