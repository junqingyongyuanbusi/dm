import json

from social_reply.application.account_management.oauth import common


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    async def set(self, key, value, ex=None):
        self.values[str(key)] = str(value).encode()

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def aclose(self):
        pass


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.key = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, key):
        self.key = str(key)

    def delete(self, key):
        self.key = str(key)

    async def execute(self):
        value = self.redis.values.pop(self.key, None)
        return value, int(value is not None)


async def test_oauth_state_is_encrypted_and_consumed_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(common, "oauth_redis", lambda: redis)

    payload = {"request_token_secret": "sensitive", "xchat_pin": "1234"}
    await common.store_oauth_state("x", "request-token", payload)

    raw = redis.values["oauth:x:request-token"]
    assert b"sensitive" not in raw
    assert b"1234" not in raw
    assert "__encrypted__" in json.loads(raw)

    assert await common.take_oauth_state("x", "request-token") == payload
    assert await common.take_oauth_state("x", "request-token") is None
