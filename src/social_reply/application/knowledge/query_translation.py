"""查询翻译回退：非英语检索未命中时，把客户查询译成英语重检英语知识库。

安全约束（与 docs/multilingual-oss-research.md 证据一致）：
- 只翻译查询，永不翻译答案——译文仅用于召回，客户可见内容始终来自
  英语批准答案的生成路径，机翻文本不会成为面向用户的事实源。
- 邮箱/URL/@handle/长号码等接触面实体先按 span 换成逐出现编号的占位符
  （同一实体出现多次、长短子串重叠都能正确对位）再送译；
  译文占位符还原不一致时整体丢弃回退（fail-closed，不影响原始评估）。
- 翻译调用任何异常都视为"无回退"，原始检索评估继续生效。
"""

import logging
import re

from social_reply.domain.reply.guard import contact_values, redact_pii
from social_reply.domain.reply.llm import LLMClient

logger = logging.getLogger(__name__)

_PLACEHOLDER_TEMPLATE = "__QTP_{}__"
_PLACEHOLDER_PATTERN = re.compile(r"__QTP_\d+__")


def _value_spans(text: str, values: tuple[str, ...]) -> list[tuple[int, int]]:
    """把 contact_values 的匹配值还原为原文 span：逐出现定位、按起点排序、丢弃重叠的较短者。"""
    spans: list[tuple[int, int]] = []
    for value in values:
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value)))
            start = text.find(value, start + 1)
    spans.sort(key=lambda span: (span[0], -(span[1] - span[0])))
    non_overlapping: list[tuple[int, int]] = []
    cursor = -1
    for start, end in spans:
        if start < cursor:
            continue
        non_overlapping.append((start, end))
        cursor = end
    return non_overlapping


def _protect_contact_values(text: str) -> tuple[str, tuple[str, ...]]:
    spans = _value_spans(text, contact_values(text))
    parts: list[str] = []
    values: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        parts.append(text[cursor:start])
        parts.append(_PLACEHOLDER_TEMPLATE.format(index))
        values.append(text[start:end])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts), tuple(values)


def _restore_contact_values(translated: str, values: tuple[str, ...]) -> str | None:
    restored = translated
    for index, value in enumerate(values):
        placeholder = _PLACEHOLDER_TEMPLATE.format(index)
        if restored.count(placeholder) != 1:
            return None
        restored = restored.replace(placeholder, value)
    if _PLACEHOLDER_PATTERN.search(restored):
        return None
    return restored


async def translate_query_to_english(llm: LLMClient, text: str) -> str | None:
    """把客户查询译成英语；LLM 不支持翻译、调用失败或还原不一致时返回 None。"""
    if not text.strip():
        return None
    translate = getattr(llm, "translate_to_english", None)
    if translate is None:
        return None
    protected, values = _protect_contact_values(text)
    protected = redact_pii(protected)
    try:
        translated = await translate(protected)
    except Exception:
        logger.exception("query translation failed; skipping retrieval fallback")
        return None
    if not translated:
        return None
    restored = _restore_contact_values(translated.strip(), values)
    if restored is None or not restored.strip():
        logger.warning("query translation placeholder restoration mismatch; fallback discarded")
        return None
    return restored
