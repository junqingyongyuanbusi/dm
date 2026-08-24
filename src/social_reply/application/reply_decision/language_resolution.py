"""客户语言解析级联：当前消息确定性检测 → LLM 判定当前消息 → 历史降级 → und。

产品语义是「收到什么语言就回什么语言」，级联顺序即这条语义的实现：判定依据永远
是客户当前这条消息，历史排在最后且只在当前消息自身给不出语种时才生效。

`domain/reply/language.py` 必须保持纯同步与确定性——它同时服务于输出闸门、知识
导入的语料语言判定和投递前校验，任何行为漂移都会波及这些路径。因此需要网络调用的
判定放在应用层，由本模块编排。

全程 fail-closed：LLM 不具备该能力、调用失败、或返回非法标签，都退到历史或原始的
und 结果，上游按现有的 UNKNOWN_LANGUAGE 转人工处理。
"""

import logging

from social_reply.domain.reply.guard import redact_pii
from social_reply.domain.reply.language import (
    AMBIGUOUS_CHINESE,
    LanguageDetection,
    detect_language,
    detect_language_from_history,
    has_detectable_letters,
)
from social_reply.domain.reply.llm import LLMClient

logger = logging.getLogger(__name__)

LLM_FALLBACK_SOURCE = "llm_fallback"

# 需要二次确认的近亲语言：lingua 在天城文上只在 hi/mr 之间二选一，短文本会给出
# 高置信度的错误答案，且置信度无法识别这类错误——实测「नमस्ते」误判成 mr 时置信度
# 0.624，反而高于同批正确判定的俄语 0.383、西语 0.340、英语 0.223。因此按候选集
# 而非置信度阈值处理：该语对实测 lingua 5/7、LLM 7/7。
# 其余多候选脚本（阿拉伯文、西里尔文、拉丁文）实测未发现同类错误，按 YAGNI 不纳入；
# 若日后出现新的近亲误判，在此登记即可，无需改动检测层。
_LOW_TRUST_TAGS = frozenset({"hi", "mr"})


def _needs_llm_review(detection: LanguageDetection) -> bool:
    return detection.tag.split("-", 1)[0] in _LOW_TRUST_TAGS


async def _attest_with_llm(llm: LLMClient | None, text: str | None) -> LanguageDetection | None:
    """让模型判定这段文本的语种；不可用或判不出时返回 None。"""
    if llm is None:
        return None
    detect = getattr(llm, "detect_language_tag", None)
    if detect is None:
        return None
    try:
        tag = await detect(redact_pii(text or ""))
    except Exception:
        logger.exception("language attestation failed; falling back")
        return None
    if not tag:
        return None
    # confidence/margin 置 1.0 与既有的脚本直判路径同约定——它们都不是分类器概率，
    # 真正的来源信息由 source 承载并落库到 reply_decisions.request_language_source。
    return LanguageDetection(tag=tag, confidence=1.0, margin=1.0, source=LLM_FALLBACK_SOURCE)


async def resolve_customer_language(
    text: str | None,
    history: tuple[tuple[str, str], ...] = (),
    *,
    llm: LLMClient | None = None,
) -> LanguageDetection:
    """判定客户当前这条消息的语种，供下游用同一语言回复。

    级联四级，前一级给出答案就不进入下一级：

    1. 当前消息的确定性检测。绝大多数消息在此结束，零额外调用。
    2. 模型判定当前消息。确定性检测判不出（短消息、拉丁语系词数不足、覆盖范围外
       的语言），或判出的是已知易混的近亲语言时启动。
    3. 历史降级。当前消息压根不含语种信息（"OK"、纯符号）且模型也没能判出时，
       沿用客户此前用过的语言，好过直接转人工。
    4. 仍然判不出 → und，上游按 UNKNOWN_LANGUAGE 转人工。

    返回值的 source 字段区分来源：current_message / llm_fallback /
    recent_user_history / unknown，落库供事后核查。
    """
    current = detect_language(text)

    # 泛 zh 已经确定是中文，只差简繁。简繁在短问候里字形相同，模型同样判不出来，
    # 不值得一次调用；客户此前自己用过的字体才是最准的线索。
    if current.tag == AMBIGUOUS_CHINESE:
        return detect_language_from_history(history, chinese_script_only=True) or current

    if current.is_reliable and not _needs_llm_review(current):
        return current

    if has_detectable_letters(text):
        # 纯 emoji / 纯数字 / 只有链接的消息没有语种可判，不浪费一次模型调用。
        attested = await _attest_with_llm(llm, text)
        if attested is not None:
            logger.info(
                "language attested by LLM: tag=%s deterministic=%s", attested.tag, current.tag
            )
            return attested

    # 确定性检测已经给出可靠答案（只是近亲语言没能复核）时不得回退历史：一条明确
    # 判出天城文的消息，绝不能被历史里的英语顶掉。
    if not current.is_reliable:
        from_history = detect_language_from_history(history)
        if from_history is not None:
            return from_history
    return current
