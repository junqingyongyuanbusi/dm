"""客户语言解析：当前消息确定性检测 → 当前消息 LLM 兜底 → fail-closed。

`domain/reply/language.py` 必须保持纯同步与确定性——它同时服务于输出闸门、
知识导入的语料语言判定和投递前校验，任何行为漂移都会波及这些路径。因此需要
网络调用的兜底判定放在应用层，由本模块编排。

当前消息的语言证据永远优先于历史：短英文等有实义字母但确定性检测判不出的
消息先交给 LLM，失败后保持 und 并转人工，绝不继承一条可能不同语言的旧消息。
只有当前消息完全没有语言信号（纯 emoji、数字或链接）时，确定性层才参考历史。
"""

import logging

from social_reply.domain.reply.language import (
    LanguageDetection,
    detect_customer_language,
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


def _needs_confirmation(detection: LanguageDetection) -> bool:
    return detection.tag.split("-", 1)[0] in _LOW_TRUST_TAGS


async def resolve_customer_language(
    text: str | None,
    history: tuple[tuple[str, str], ...] = (),
    *,
    llm: LLMClient | None = None,
) -> LanguageDetection:
    """判定客户消息语种，当前消息优先、LLM 兜底。

    两种情况会走 LLM：当前消息含实义字母但确定性检测判不出（und），或判出的是
    已知易混的近亲语言。历史结果只可能来自无语言信号的当前消息，或用于细化通用
    中文标签的简繁体。返回结果的 source 字段区分 current_message、
    recent_user_history 与 llm_fallback，下游据此决定输出闸门强度。
    """
    deterministic = detect_customer_language(text, history)
    if deterministic.is_reliable and not _needs_confirmation(deterministic):
        return deterministic
    if llm is None or not has_detectable_letters(text):
        # 纯 emoji / 纯数字 / 只有链接的消息没有当前语种可判，允许确定性层沿用历史；
        # 有字母但模型不可用时则保持当前消息的 und，绝不让历史覆盖本轮语言。
        return deterministic

    detect = getattr(llm, "detect_language_tag", None)
    if detect is None:
        return deterministic
    try:
        tag = await detect(text or "")
    except Exception:
        logger.exception("language fallback failed; keeping deterministic result")
        return deterministic
    if not tag:
        return deterministic

    logger.info("language resolved by LLM: tag=%s deterministic=%s", tag, deterministic.tag)
    # confidence/margin 置 1.0 与既有的脚本直判路径同约定——它们都不是分类器概率，
    # 真正的来源信息由 source 承载并落库到 reply_decisions.request_language_source。
    return LanguageDetection(tag=tag, confidence=1.0, margin=1.0, source=LLM_FALLBACK_SOURCE)
