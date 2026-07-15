from social_reply.application.reply_decision import runner


def test_killswitch_client_is_reused():
    c1 = runner._get_redis()
    c2 = runner._get_redis()
    assert c1 is c2  # 模块级共享，不每次 from_url 建新连接池
