import pytest

from social_reply.application.knowledge.query_translation import (
    _protect_contact_values,
    _restore_contact_values,
    translate_query_to_english,
)


class _EchoTranslator:
    def __init__(self, translated: str | None):
        self.translated = translated
        self.received: str | None = None

    async def translate_to_english(self, text: str) -> str | None:
        self.received = text
        return self.translated


def test_protect_and_restore_repeated_contact_value() -> None:
    text = "My email is test@example.com; please confirm test@example.com."
    protected, values = _protect_contact_values(text)

    assert protected.count("__QTP_") == 2
    assert len(values) == 2
    assert _restore_contact_values(protected, values) == text


def test_protect_prefers_non_overlapping_longer_entity_span() -> None:
    text = "The order number is 1234567890, not 1234567."
    protected, values = _protect_contact_values(text)
    restored = _restore_contact_values(protected, values)

    assert restored == text
    assert values[0] == "1234567890"
    assert "1234567" in values


@pytest.mark.asyncio
async def test_missing_placeholder_discards_translation() -> None:
    translator = _EchoTranslator("The translated query omitted the protected value.")

    result = await translate_query_to_english(translator, "Email test@example.com please")

    assert result is None


@pytest.mark.asyncio
async def test_translation_returns_restored_raw_entity_for_lexical_retrieval() -> None:
    translator = _EchoTranslator("English query __QTP_0__")

    result = await translate_query_to_english(translator, "邮箱是 test@example.com 吗？")

    assert result == "English query test@example.com"
    assert translator.received is not None
    assert "test@example.com" not in translator.received
