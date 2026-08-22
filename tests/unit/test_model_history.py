"""喂给模型的历史上下文过滤：只保留形成过问—答配对的轮次。"""

from social_reply.application.reply_decision.runner import _answered_turns


def test_answered_pairs_are_kept():
    history = (
        ("user", "想买 A 套餐"),
        ("assistant", "已记录"),
    )
    assert _answered_turns(history) == history


def test_trailing_unanswered_customer_messages_are_dropped():
    # 这是线上的真实形态：连续几条客户消息都被转人工、从无回复。
    history = (
        ("user", "怎么查券商牌照？"),
        ("assistant", "在 WikiFX 搜索该券商并打开详情页。"),
        ("user", "金融庁のライセンスあるって言ってるけど、WikiFXには出てこない。"),
        ("user", "こんにちは"),
    )
    assert _answered_turns(history) == (
        ("user", "怎么查券商牌照？"),
        ("assistant", "在 WikiFX 搜索该券商并打开详情页。"),
    )


def test_unanswered_message_in_the_middle_is_dropped():
    history = (
        ("user", "问题一"),
        ("user", "问题二"),
        ("assistant", "回答二"),
        ("user", "问题三"),
    )
    assert _answered_turns(history) == (("user", "问题二"), ("assistant", "回答二"))


def test_assistant_turns_are_always_kept():
    # 窗口边界上可能只截到助手消息，其提问在窗口之外——保留即可，不做额外推断。
    history = (("assistant", "上一轮的回复"), ("user", "新问题"))
    assert _answered_turns(history) == (("assistant", "上一轮的回复"),)


def test_empty_history_is_unchanged():
    assert _answered_turns(()) == ()


def test_history_without_any_answer_becomes_empty():
    history = (("user", "a"), ("user", "b"), ("user", "c"))
    assert _answered_turns(history) == ()
