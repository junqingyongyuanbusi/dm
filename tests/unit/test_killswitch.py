from social_reply.infrastructure.killswitch import KillSwitchChecker


class _FakeRedis:
    def __init__(self, present: set[str]):
        self._present = present

    async def mget(self, keys):
        return [b"1" if k in self._present else None for k in keys]


async def test_no_flags_enabled():
    checker = KillSwitchChecker(_FakeRedis(set()))
    assert await checker.is_disabled("b1", "acc1") is False


async def test_global_flag_disables_all():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:global:default"}))
    assert await checker.is_disabled("b1", "acc1") is True


async def test_brand_flag_disables_brand():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:brand:default:b1"}))
    assert await checker.is_disabled("b1", "acc1") is True
    assert await checker.is_disabled("b2", "acc1") is False


async def test_account_flag_disables_account():
    checker = KillSwitchChecker(_FakeRedis({"killswitch:account:default:acc1"}))
    assert await checker.is_disabled("b1", "acc1") is True
    assert await checker.is_disabled("b1", "acc2") is False
