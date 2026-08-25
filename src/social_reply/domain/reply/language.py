"""Fail-closed language detection for customer-facing reply enforcement."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from functools import cache

import hanzidentifier
from lingua import Language, LanguageDetector, LanguageDetectorBuilder

UNKNOWN_LANGUAGE = "und"
# 只知道是中文、还没定简繁的标签。它本身可靠但不完整，需要额外线索细化。
AMBIGUOUS_CHINESE = "zh"
_CHINESE_SCRIPT_TAGS = frozenset({"zh-Hans", "zh-Hant"})
_HISTORY_LOOKBACK = 3
_MIN_LETTERS = 2
_MIN_CONFIDENCE = 0.20
_MIN_MARGIN = 0.10
_REDACTION_PLACEHOLDER = re.compile(r"\[REDACTED_[A-Z_]+\]")
_LANGUAGE_NEUTRAL_TOKEN = re.compile(r"(?i)[A-Z0-9_.+-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+|@[A-Z0-9_.-]+")
_URL_PREFIX = re.compile(r"(?i)https?://|www\.")
_URL_ALLOWED_PUNCTUATION = frozenset(":/?&=._-%#@+~")

_LATIN_LANGUAGES = (
    Language.AFRIKAANS,
    Language.ALBANIAN,
    Language.BASQUE,
    Language.BOKMAL,
    Language.BOSNIAN,
    Language.CATALAN,
    Language.CROATIAN,
    Language.CZECH,
    Language.DANISH,
    Language.DUTCH,
    Language.ENGLISH,
    Language.ESTONIAN,
    Language.FINNISH,
    Language.FRENCH,
    Language.GERMAN,
    Language.HUNGARIAN,
    Language.ICELANDIC,
    Language.INDONESIAN,
    Language.IRISH,
    Language.ITALIAN,
    Language.LATVIAN,
    Language.LITHUANIAN,
    Language.MALAY,
    Language.POLISH,
    Language.PORTUGUESE,
    Language.ROMANIAN,
    Language.SLOVAK,
    Language.SLOVENE,
    Language.SOMALI,
    Language.SPANISH,
    Language.SWAHILI,
    Language.SWEDISH,
    Language.TAGALOG,
    Language.TURKISH,
    Language.VIETNAMESE,
    Language.WELSH,
    Language.AZERBAIJANI,
    Language.ESPERANTO,
    Language.GANDA,
    Language.LATIN,
    Language.MAORI,
    Language.NYNORSK,
    Language.SERBIAN,
    Language.SHONA,
    Language.SOTHO,
    Language.TSONGA,
    Language.TSWANA,
    Language.XHOSA,
    Language.YORUBA,
    Language.ZULU,
)
_CYRILLIC_LANGUAGES = (
    Language.BELARUSIAN,
    Language.BULGARIAN,
    Language.KAZAKH,
    Language.MACEDONIAN,
    Language.MONGOLIAN,
    Language.RUSSIAN,
    Language.SERBIAN,
    Language.UKRAINIAN,
)
_ARABIC_SCRIPT_LANGUAGES = (Language.ARABIC, Language.PERSIAN, Language.URDU)
_DEVANAGARI_LANGUAGES = (Language.HINDI, Language.MARATHI)
# 文字系统与语言一对一的脚本，判出脚本即判出语言，无需分类器。
_DIRECT_SCRIPT_LANGUAGES = {
    "gurmukhi": "pa",
    "gujarati": "gu",
    "odia": "or",
    "tamil": "ta",
    "telugu": "te",
    "kannada": "kn",
    "malayalam": "ml",
    "sinhala": "si",
    "lao": "lo",
    "myanmar": "my",
    "khmer": "km",
    "armenian": "hy",
    "georgian": "ka",
}
_NEPALI_HINTS = ("कसरी", "गर्न", "सक्छ", "छन्", "छु", "तपाईं")
_YIDDISH_HINTS = ("ווי", "קען", "געלט", "זיי", "ניט")

_CHINESE_GENERAL_HINTS = (
    "你好",
    "您好",
    "的",
    "怎么",
    "怎麼",
    "如何",
    "多久",
    "客服",
    "联系",
    "聯繫",
    "退款",
    "到账",
    "到賬",
    "可以",
    "为什么",
    "為什麼",
)


@dataclass(frozen=True)
class LanguageDetection:
    tag: str
    confidence: float = 0.0
    margin: float = 0.0
    source: str = "unknown"

    @property
    def is_known(self) -> bool:
        """A tag is known only after detect_language has applied its path-specific gate."""
        return self.tag != UNKNOWN_LANGUAGE

    @property
    def is_reliable(self) -> bool:
        return self.is_known and self.confidence >= _MIN_CONFIDENCE and self.margin >= _MIN_MARGIN


@cache
def _detector(languages: tuple[Language, ...]) -> LanguageDetector:
    return LanguageDetectorBuilder.from_languages(*languages).build()


def _script_of_letter(char: str) -> str:
    value = ord(char)
    if 0x3040 <= value <= 0x30FF:
        return "kana"
    if 0xAC00 <= value <= 0xD7AF:
        return "hangul"
    if 0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF:
        return "han"
    if 0x0E00 <= value <= 0x0E7F:
        return "thai"
    if 0x0600 <= value <= 0x06FF:
        return "arabic"
    if 0x0900 <= value <= 0x097F:
        return "devanagari"
    if 0x0400 <= value <= 0x052F:
        return "cyrillic"
    if 0x0370 <= value <= 0x03FF:
        return "greek"
    if 0x0980 <= value <= 0x09FF:
        return "bengali"
    if 0x0590 <= value <= 0x05FF:
        return "hebrew"
    if 0x0A00 <= value <= 0x0A7F:
        return "gurmukhi"
    if 0x0A80 <= value <= 0x0AFF:
        return "gujarati"
    if 0x0B00 <= value <= 0x0B7F:
        return "odia"
    if 0x0B80 <= value <= 0x0BFF:
        return "tamil"
    if 0x0C00 <= value <= 0x0C7F:
        return "telugu"
    if 0x0C80 <= value <= 0x0CFF:
        return "kannada"
    if 0x0D00 <= value <= 0x0D7F:
        return "malayalam"
    if 0x0D80 <= value <= 0x0DFF:
        return "sinhala"
    if 0x0E80 <= value <= 0x0EFF:
        return "lao"
    if 0x1000 <= value <= 0x109F:
        return "myanmar"
    if 0x1200 <= value <= 0x137F:
        return "ethiopic"
    if 0x1780 <= value <= 0x17FF:
        return "khmer"
    if 0x0530 <= value <= 0x058F:
        return "armenian"
    if 0x10A0 <= value <= 0x10FF:
        return "georgian"
    if "LATIN" in unicodedata.name(char, ""):
        return "latin"
    return "other"


def _letter_scripts(text: str) -> Counter[str]:
    return Counter(
        _script_of_letter(char) for char in text if unicodedata.category(char).startswith("L")
    )


def _language_tag(language: Language) -> str:
    iso_code = language.iso_code_639_1
    if iso_code is not None:
        return iso_code.name.lower()
    return language.iso_code_639_3.name.lower()


def _detect_with_candidates(
    text: str,
    languages: tuple[Language, ...],
    *,
    source: str,
    min_confidence: float = _MIN_CONFIDENCE,
    min_margin: float = _MIN_MARGIN,
) -> LanguageDetection:
    values = _detector(languages).compute_language_confidence_values(text)
    if not values:
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
    top = values[0]
    second_value = values[1].value if len(values) > 1 else 0.0
    margin = top.value - second_value
    if top.value < min_confidence or margin < min_margin:
        return LanguageDetection(
            tag=UNKNOWN_LANGUAGE,
            confidence=top.value,
            margin=margin,
            source="unknown",
        )
    return LanguageDetection(
        tag=_language_tag(top.language),
        confidence=top.value,
        margin=margin,
        source=source,
    )


def _detect_chinese_variant(text: str, *, source: str) -> LanguageDetection:
    simplified = hanzidentifier.is_simplified(text)
    traditional = hanzidentifier.is_traditional(text)
    if simplified and not traditional:
        return LanguageDetection(tag="zh-Hans", confidence=1.0, margin=1.0, source=source)
    if traditional and not simplified:
        return LanguageDetection(tag="zh-Hant", confidence=1.0, margin=1.0, source=source)
    if any(hint in text for hint in _CHINESE_GENERAL_HINTS):
        return LanguageDetection(tag="zh", confidence=0.8, margin=0.8, source=source)
    return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")


def _strip_language_neutral_tokens(text: str) -> str:
    stripped = _LANGUAGE_NEUTRAL_TOKEN.sub(" ", text)
    while True:
        match = _URL_PREFIX.search(stripped)
        if match is None:
            return stripped
        end = match.end()
        while end < len(stripped):
            char = stripped[end]
            if ord(char) > 127:
                break
            category = unicodedata.category(char)
            if char.isspace() or category.startswith("Z"):
                break
            if category.startswith("P") and char not in _URL_ALLOWED_PUNCTUATION:
                break
            end += 1
        stripped = f"{stripped[: match.start()]} {stripped[end:]}"


def _normalize_for_detection(text: str | None) -> str:
    """脱掉脱敏占位符与语言中立 token（邮箱/URL/@handle），只留可判定语种的文本。"""
    normalized = _REDACTION_PLACEHOLDER.sub(" ", (text or ""))
    return _strip_language_neutral_tokens(normalized).strip()


def has_detectable_letters(text: str | None) -> bool:
    """是否含足够的实义字母，值得为它做语言判定。

    与 detect_language 共用同一套归一化和最小字母数，因此二者判断一致：
    本函数为 False 时 detect_language 必然返回 und。纯 emoji、纯数字、
    只有邮箱/链接的消息据此被挡在 LLM 兜底判定之外，不浪费模型调用。
    """
    return sum(_letter_scripts(_normalize_for_detection(text)).values()) >= _MIN_LETTERS


def detect_language(text: str | None, *, source: str = "current_message") -> LanguageDetection:
    normalized = _normalize_for_detection(text)
    scripts = _letter_scripts(normalized)
    letter_count = sum(scripts.values())
    if letter_count < _MIN_LETTERS:
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")

    dominant_script, dominant_count = scripts.most_common(1)[0]
    dominant_ratio = dominant_count / letter_count
    kana_and_han = scripts["kana"] + scripts["han"]
    if dominant_script == "kana" or (
        scripts["kana"] >= 2
        and kana_and_han / letter_count >= 0.8
        and scripts["kana"] / kana_and_han >= 0.2
    ):
        return LanguageDetection(tag="ja", confidence=1.0, margin=1.0, source=source)
    if dominant_script == "hangul" and dominant_ratio >= 0.6:
        return LanguageDetection(tag="ko", confidence=1.0, margin=1.0, source=source)
    if dominant_ratio < 0.6:
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
    if dominant_script == "han":
        return _detect_chinese_variant(normalized, source=source)
    if dominant_script == "thai":
        return LanguageDetection(tag="th", confidence=1.0, margin=1.0, source=source)
    if dominant_script == "arabic":
        return _detect_with_candidates(normalized, _ARABIC_SCRIPT_LANGUAGES, source=source)
    if dominant_script == "cyrillic":
        return _detect_with_candidates(normalized, _CYRILLIC_LANGUAGES, source=source)
    if dominant_script == "greek":
        return LanguageDetection(tag="el", confidence=1.0, margin=1.0, source=source)
    if dominant_script == "devanagari":
        if any(hint in normalized for hint in _NEPALI_HINTS):
            return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
        return _detect_with_candidates(normalized, _DEVANAGARI_LANGUAGES, source=source)
    if dominant_script == "bengali":
        if any(char in normalized for char in "ৰৱ"):
            return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
        return LanguageDetection(tag="bn", confidence=1.0, margin=1.0, source=source)
    if dominant_script == "hebrew":
        if any(char in normalized for char in "ײױװ") or any(
            hint in normalized for hint in _YIDDISH_HINTS
        ):
            return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
        return LanguageDetection(tag="he", confidence=1.0, margin=1.0, source=source)
    if dominant_script == "ethiopic":
        # Amharic and Tigrinya share this script; fail closed without a language classifier.
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="script_ambiguous")
    if dominant_script in _DIRECT_SCRIPT_LANGUAGES:
        return LanguageDetection(
            tag=_DIRECT_SCRIPT_LANGUAGES[dominant_script],
            confidence=1.0,
            margin=1.0,
            source=source,
        )
    if dominant_script == "other":
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
    if dominant_script != "latin":
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")

    words = re.findall(r"[^\W\d_]+", normalized, flags=re.UNICODE)
    if len(words) < 2:
        return LanguageDetection(tag=UNKNOWN_LANGUAGE, source="unknown")
    min_confidence = 0.08 if len(words) >= 4 else _MIN_CONFIDENCE
    min_margin = 0.04 if len(words) >= 4 else _MIN_MARGIN
    return _detect_with_candidates(
        normalized,
        _LATIN_LANGUAGES,
        source=source,
        min_confidence=min_confidence,
        min_margin=min_margin,
    )


def detect_language_from_history(
    history: tuple[tuple[str, str], ...],
    *,
    chinese_script_only: bool = False,
) -> LanguageDetection | None:
    """从最近几条客户消息回推语种；没有可靠结果时返回 None。

    这是降级手段，不是判定依据：客户当前这条消息用什么语言就该回什么语言，历史
    只在当前消息自身给不出语种时才有资格发言，调用方负责守住这个前提。曾经把它
    嵌在当前消息的检测里，结果客户从日语切到英语说 "hello" 时被历史判成日语。

    chinese_script_only 用于当前消息已确定是中文、只差简繁的场合：此时必须丢弃
    非中文的历史结果，否则会把一条确定是中文的消息判成别的语言。
    """
    recent = [
        message for role, message in history if role == "user" and message and message.strip()
    ][-_HISTORY_LOOKBACK:]
    for message in reversed(recent):
        detected = detect_language(message, source="recent_user_history")
        if not detected.is_reliable:
            continue
        if chinese_script_only and detected.tag not in _CHINESE_SCRIPT_TAGS:
            continue
        return detected
    return None


def languages_match(expected: str, observed: str) -> bool:
    if expected == UNKNOWN_LANGUAGE or observed == UNKNOWN_LANGUAGE:
        return False
    expected_parts = expected.casefold().split("-", 1)
    observed_parts = observed.casefold().split("-", 1)
    if expected_parts[0] != observed_parts[0]:
        return False
    if expected_parts[0] != "zh":
        return True
    if len(expected_parts) == 1 or len(observed_parts) == 1:
        return True
    return expected_parts[1] == observed_parts[1]


# detect_language 可能产出的全部标签。输出闸门据此决定能否复核回复语种：表内的
# 标签可以严格校验；表外的（例如模型判出的 ne、am）确定性检测本就判不出来，严格
# 校验必然误杀，只能退到文字系统一致性。由各候选表推导而来，不手工维护第二份清单。
_DETERMINISTIC_TAGS: frozenset[str] = frozenset(
    {"ja", "ko", AMBIGUOUS_CHINESE, "th", "el", "he", "bn"}
    | _CHINESE_SCRIPT_TAGS
    | set(_DIRECT_SCRIPT_LANGUAGES.values())
    | {
        _language_tag(language)
        for group in (
            _LATIN_LANGUAGES,
            _CYRILLIC_LANGUAGES,
            _ARABIC_SCRIPT_LANGUAGES,
            _DEVANAGARI_LANGUAGES,
        )
        for language in group
    }
)


def is_deterministically_verifiable(tag: str) -> bool:
    """detect_language 是否可能产出该标签——即输出闸门能否复核回复真的用了它。"""
    return tag in _DETERMINISTIC_TAGS


# 各语言允许出现的文字系统。这张表天然追不上世界上的语言——例如它收了 ru/uk/bg
# 却漏了同用西里尔文的 mk/sr/be/kk/mn，导致这些能被正确检测的语言反被闸门误杀。
# 因此调用方可用 allowed_scripts 参数直接给出期望集合（通常取客户原文的主导文字
# 系统），本表只作为无上下文时的回退。
_LANGUAGE_ALLOWED_SCRIPTS: dict[str, frozenset[str]] = {
    "zh": frozenset({"han", "latin"}),
    "ja": frozenset({"kana", "han", "latin"}),
    "ko": frozenset({"hangul", "han", "latin"}),
    "ar": frozenset({"arabic", "latin"}),
    "fa": frozenset({"arabic", "latin"}),
    "ur": frozenset({"arabic", "latin"}),
    "ru": frozenset({"cyrillic", "latin"}),
    "uk": frozenset({"cyrillic", "latin"}),
    "bg": frozenset({"cyrillic", "latin"}),
    "th": frozenset({"thai", "latin"}),
    "el": frozenset({"greek", "latin"}),
    "hi": frozenset({"devanagari", "latin"}),
    "mr": frozenset({"devanagari", "latin"}),
    "bn": frozenset({"bengali", "latin"}),
    "he": frozenset({"hebrew", "latin"}),
    "pa": frozenset({"gurmukhi", "latin"}),
    "gu": frozenset({"gujarati", "latin"}),
    "ta": frozenset({"tamil", "latin"}),
    "te": frozenset({"telugu", "latin"}),
    "kn": frozenset({"kannada", "latin"}),
    "ml": frozenset({"malayalam", "latin"}),
    "or": frozenset({"odia", "latin"}),
    "si": frozenset({"sinhala", "latin"}),
    "lo": frozenset({"lao", "latin"}),
    "my": frozenset({"myanmar", "latin"}),
    "am": frozenset({"ethiopic", "latin"}),
    "km": frozenset({"khmer", "latin"}),
    "hy": frozenset({"armenian", "latin"}),
    "ka": frozenset({"georgian", "latin"}),
}


def dominant_script(text: str | None) -> str:
    """文本的主导文字系统（如 latin / han / cyrillic）；无主导或无字母时返回空串。

    阈值与 detect_language 的 dominant_ratio 一致，二者对"这段文本属于哪套书写系统"
    的判断因此保持同一口径。
    """
    scripts = _letter_scripts(_normalize_for_detection(text))
    total = sum(scripts.values())
    if total == 0:
        return ""
    script, count = scripts.most_common(1)[0]
    return script if count / total >= 0.6 else ""


def expected_scripts_for(text: str | None) -> frozenset[str] | None:
    """由客户原文推导回复额外允许的文字系统：主导文字系统 ∪ 拉丁（品牌名、URL 等）。

    结果与按语言查表的结果取并集，只放宽不收紧——日语这类混合书写系统的语言，
    客户原文的主导脚本可能只是 kana，绝不能因此把回复里的 han 判成越界。
    判不出主导文字系统时返回 None。
    """
    script = dominant_script(text)
    return frozenset({script, "latin"}) if script else None


def reply_language_matches(
    expected: str,
    text: str,
    *,
    extra_allowed_scripts: frozenset[str] | None = None,
) -> tuple[bool, str]:
    observed_detection = detect_language(text)
    observed = observed_detection.tag
    if not observed_detection.is_reliable or not languages_match(expected, observed):
        return False, observed

    primary = expected.split("-", 1)[0].casefold()
    allowed_scripts = _LANGUAGE_ALLOWED_SCRIPTS.get(primary, frozenset({"latin"}))
    if extra_allowed_scripts:
        allowed_scripts = allowed_scripts | extra_allowed_scripts

    sentence_safe = _strip_language_neutral_tokens(text)
    sentence_safe = re.sub(r"(?<=\d)[.,](?=\d)", ":", sentence_safe)
    fragments = re.split(r"[.!?。！？；;\n]+", sentence_safe)
    for fragment in fragments:
        cleaned_fragment = fragment.strip()
        letter_count = sum(unicodedata.category(char).startswith("L") for char in cleaned_fragment)
        if letter_count < _MIN_LETTERS:
            continue
        # 文字系统越界对短片段同样有判别力（"你好"、"Спасибо" 都判得出），先拦这一层。
        fragment_script = dominant_script(cleaned_fragment)
        if fragment_script and fragment_script not in allowed_scripts:
            return False, observed
        fragment_detection = detect_language(cleaned_fragment)
        # 判不出语种 ≠ 语种错了。问候语和短承接句在任何门槛下都判不出来（实测 "Hello"
        # 的 top1 是 st、"Sure" 是 fr、"No problem" 是 bs），把"判不出"当违规会误杀
        # 11.6% 的已审核英文答案——它们逐字发送也过不了闸门，永远只能转人工。
        # 回错语言这个唯一需要拦住的错误，由「片段可靠地检出了另一种语言」负责。
        if fragment_detection.is_reliable and not languages_match(expected, fragment_detection.tag):
            return False, observed

    neutral_stripped = _strip_language_neutral_tokens(text)
    scripts = _letter_scripts(neutral_stripped)
    total = sum(scripts.values())
    if total == 0:
        return True, observed
    disallowed = sum(count for script, count in scripts.items() if script not in allowed_scripts)
    if disallowed / total > 0.1:
        return False, observed
    if primary not in {"en", "es", "fr", "de", "pt", "it", "nl", "pl", "tr"}:
        latin = scripts["latin"]
        non_latin = total - latin
        if non_latin > 0 and latin / total > 0.35:
            return False, observed
    return True, observed


def assess_knowledge_language(question: str, reply: str) -> tuple[str, str]:
    detections = (detect_language(question), detect_language(reply))
    known_tags = [detection.tag for detection in detections if detection.is_reliable]
    primary_languages = {tag.split("-", 1)[0] for tag in known_tags}
    if not known_tags:
        return UNKNOWN_LANGUAGE, "unknown"
    if primary_languages == {"en"}:
        return ("en", "english") if len(known_tags) == 2 else ("en", "unknown")
    if "en" in primary_languages or len(primary_languages) > 1:
        return "mixed", "mixed"
    if len(primary_languages) == 1:
        return known_tags[0], "non_english"
    return UNKNOWN_LANGUAGE, "unknown"
