import json
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretStore:
    """Read legacy file:// secret references during encrypted-bundle migration."""
    def read(self, reference: str | None) -> str:
        if not reference:
            raise ValueError("missing_secret_reference")
        parsed = urlparse(reference)
        if parsed.scheme != "file":
            raise ValueError(f"unsupported_secret_scheme:{parsed.scheme}")
        path = Path(unquote(parsed.path))
        value = path.read_text().strip()
        if not value:
            raise ValueError(f"empty_secret:{path}")
        return value

    def read_mapping(self, reference: str | None, *, fallback_key: str) -> dict[str, str]:
        """读取 JSON Secret bundle；兼容 Telegram 现有的纯文本 Secret 文件。"""
        value = self.read(reference)
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {fallback_key: value}
        if not isinstance(decoded, dict):
            raise ValueError("secret_bundle_must_be_object")
        result = {str(key): str(item) for key, item in decoded.items() if item is not None}
        if not result:
            raise ValueError("empty_secret_bundle")
        return result


secret_store = SecretStore()
