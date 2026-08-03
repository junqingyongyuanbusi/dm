FEISHU_API_BASE_URL = "https://open.feishu.cn"
FEISHU_GROUP_MODE = "mentions_only"
FEISHU_APP_ID_PATTERN = r"^cli_[A-Za-z0-9]{8,64}$"


def nonblank_string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None
