"""Encrypt legacy file or plaintext database secrets into shared envelopes.

Legacy references are deliberately retained as rollback evidence. Runtime uses only encrypted
bundles; deleting old files is a separate, operator-reviewed cleanup after backup verification.
"""

import asyncio
import json
from collections.abc import Mapping

from sqlalchemy import inspect, text

from social_reply.infrastructure.database.engine import get_engine
from social_reply.infrastructure.secret_crypto import decrypt_secret_bundle, encrypt_secret_bundle
from social_reply.infrastructure.secrets import secret_store


def _is_encrypted(value: Mapping | None) -> bool:
    if not value or "__encrypted__" not in value:
        return False
    decrypt_secret_bundle(value)
    return True


def _file_values(
    reference: str, *, fallback_key: str, rename_token_to_bot_token: bool = False
) -> dict[str, str]:
    values = secret_store.read_mapping(reference, fallback_key=fallback_key)
    if rename_token_to_bot_token and set(values) == {"token"}:
        return {"bot_token": values["token"]}
    return values


async def migrate() -> tuple[int, int, int]:
    engine = get_engine()
    async with engine.begin() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                table: {column["name"] for column in inspect(sync).get_columns(table)}
                for table in ("platform_apps", "platform_accounts", "provisioning_jobs")
            }
        )
        counts = [0, 0, 0]
        specs = (
            ("platform_apps", "credential_bundle", "credential_ref", "secret", 0, False),
            ("platform_accounts", "credential_bundle", "credential_ref", "token", 1, True),
            (
                "platform_accounts",
                "webhook_secret_bundle",
                "webhook_secret_ref",
                "secret",
                None,
                False,
            ),
            ("provisioning_jobs", "staging_secret", "staging_secret_ref", "token", 2, False),
        )
        for table, bundle_column, ref_column, fallback_key, count_index, rename_token in specs:
            selected = ["id", bundle_column]
            has_ref = ref_column in columns[table]
            if has_ref:
                selected.append(ref_column)
            rows = (
                await connection.execute(text(f"SELECT {', '.join(selected)} FROM {table}"))
            ).mappings()
            for row in rows:
                value = row[bundle_column]
                if _is_encrypted(value):
                    continue
                values = dict(value or {})
                reference = row.get(ref_column) if has_ref else None
                if not values and reference:
                    values = _file_values(
                        reference,
                        fallback_key=fallback_key,
                        rename_token_to_bot_token=rename_token,
                    )
                if not values:
                    continue
                await connection.execute(
                    text(f"UPDATE {table} SET {bundle_column}=CAST(:bundle AS jsonb) WHERE id=:id"),
                    {
                        "bundle": json.dumps(encrypt_secret_bundle(values)),
                        "id": row["id"],
                    },
                )
                if count_index is not None:
                    counts[count_index] += 1
    return tuple(counts)


if __name__ == "__main__":
    print(asyncio.run(migrate()))
