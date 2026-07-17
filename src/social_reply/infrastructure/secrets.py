import json
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse


class SecretStore:
    """读写 Secret 引用；当前支持 file://，生产可替换 Vault/云 Secret Manager。"""

    def write_mapping(self, path: Path, payload: dict[str, str]) -> str:
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.touch(mode=0o600, exist_ok=False)
            temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            temporary.chmod(0o600)
            temporary.replace(path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return path.as_uri()

    def delete(self, reference: str | None) -> None:
        if not reference:
            return
        parsed = urlparse(reference)
        if parsed.scheme != "file":
            raise ValueError(f"unsupported_secret_scheme:{parsed.scheme}")
        Path(unquote(parsed.path)).unlink(missing_ok=True)

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
