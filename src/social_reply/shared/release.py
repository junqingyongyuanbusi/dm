import os
import re

_RELEASE_SHA = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def current_release_sha() -> str:
    """Return the immutable image revision injected by the production Dockerfile."""
    value = os.environ.get("RELEASE_SHA", "unknown").strip()
    if not _RELEASE_SHA.fullmatch(value):
        raise RuntimeError("RELEASE_SHA is missing or invalid")
    return value
