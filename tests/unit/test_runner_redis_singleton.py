from social_reply.application.reply_decision import runner
from social_reply.infrastructure import killswitch


def test_killswitch_client_is_reused():
    killswitch._redis = None
    c1 = runner._make_killswitch()._redis
    c2 = runner._make_killswitch()._redis
    assert c1 is c2  # 模块级共享，不每次 from_url 建新连接池
