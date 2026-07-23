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
        self.commands: list[tuple[str, str]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get(self, key):
        self.commands.append(("get", str(key)))

    def delete(self, key):
        self.commands.append(("delete", str(key)))

    async def execute(self):
        results = []
        for command, key in self.commands:
            if command == "get":
                results.append(self.redis.values.get(key))
            else:
                results.append(int(self.redis.values.pop(key, None) is not None))
        return results


async def test_oauth_state_is_encrypted_and_consumed_once(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(common, "oauth_redis", lambda: redis)

    payload = {"request_token_secret": "sensitive", "xchat_pin": "1234"}
    await common.store_oauth_state("x", "request-token", payload)

    redis_key = common.oauth_state_key("x", "request-token")
    assert "request-token" not in redis_key
    assert redis_key.startswith("x:oauth1:transaction:")
    raw = redis.values[redis_key]
    assert b"sensitive" not in raw
    assert b"1234" not in raw
    assert "__encrypted__" in json.loads(raw)

    assert await common.take_oauth_state("x", "request-token") == payload
    assert await common.take_oauth_state("x", "request-token") is None


async def test_x_oauth_state_can_use_legacy_writer_during_rolling_deploy(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(common, "oauth_redis", lambda: redis)
    monkeypatch.setattr(
        common,
        "get_settings",
        lambda: type("Settings", (), {"x_oauth_legacy_state_write": True})(),
    )

    await common.store_oauth_state("x", "rolling-token", {"request_token_secret": "rolling-secret"})

    assert "oauth:x:rolling-token" in redis.values
    assert await common.take_oauth_state("x", "rolling-token") == {
        "request_token_secret": "rolling-secret"
    }
    assert redis.values == {}


async def test_x_oauth_state_consumes_predeploy_legacy_key(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(common, "oauth_redis", lambda: redis)
    payload = {"request_token_secret": "legacy-secret"}
    encrypted = common.encrypt_secret_bundle(
        {"payload": json.dumps(payload, separators=(",", ":"))}
    )
    redis.values["oauth:x:legacy-token"] = json.dumps(encrypted).encode()

    assert await common.take_oauth_state("x", "legacy-token") == payload
    assert redis.values == {}
