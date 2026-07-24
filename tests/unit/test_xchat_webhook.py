import uuid
from types import SimpleNamespace

from social_reply.application.event_ingestion import xchat_webhook


async def test_xchat_webhook_marks_key_recovery_required(monkeypatch):
    raw_id = uuid.uuid4()
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        tenant_id="default",
        external_account_id="bot-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
        },
        config={},
    )
    payload = {
        "data": {
            "event_type": "chat.received",
            "event_uuid": "event-1",
            "payload": {"sender_id": "user-1", "encoded_event": "cipher"},
        }
    }

    async def fake_account(value):
        return account

    async def fake_claim(*args):
        return xchat_webhook.XChatClaim(payload=payload, token="claim-1")

    statuses = []

    async def fake_mark(value, claim_token, status, **kwargs):
        assert claim_token == "claim-1"
        statuses.append(status)

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "_claim", fake_claim)
    monkeypatch.setattr(xchat_webhook, "_mark", fake_mark)

    await xchat_webhook.process_xchat_raw_event(raw_id, account_id)
    assert statuses == ["XCHAT_KEY_RECOVERY_REQUIRED"]


async def test_xchat_webhook_decrypts_and_ingests(monkeypatch):
    raw_id = uuid.uuid4()
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        tenant_id="default",
        external_account_id="bot-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_private_keys_b64": "private",
        },
        config={},
    )
    payload = {
        "data": {
            "event_type": "chat.received",
            "event_uuid": "event-1",
            "payload": {
                "conversation_id": "bot-1:user-1",
                "sender_id": "user-1",
                "encoded_event": "cipher",
            },
        }
    }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get_user_public_keys(self, user_id):
            return [
                {
                    "public_key_version": "7",
                    "public_key": "identity",
                    "signing_public_key": "signing",
                    "identity_public_key_signature": "binding",
                }
            ]

        async def aclose(self):
            pass

    async def fake_account(value):
        return account

    async def fake_claim(*args):
        return xchat_webhook.XChatClaim(payload=payload, token="claim-2")

    def fake_decrypt(**kwargs):
        return {
            "type": "Message",
            "message_id": "message-1",
            "sender_id": "user-1",
            "content": {"content_type": "Text", "text": "hello"},
            "verified": True,
        }

    ingested = []

    async def fake_ingest(event, *, raw_event_id, raw_event_claim_token):
        ingested.append((event, raw_event_id, raw_event_claim_token))
        return uuid.uuid4()

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "_claim", fake_claim)
    monkeypatch.setattr(xchat_webhook, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_webhook, "decrypt_live_event", fake_decrypt)
    monkeypatch.setattr(xchat_webhook, "ingest_canonical_event", fake_ingest)

    await xchat_webhook.process_xchat_raw_event(raw_id, account_id)
    assert ingested[0][0].external_event_id == "message-1"
    assert ingested[0][1] == raw_id
    assert ingested[0][2] == "claim-2"


async def test_xchat_webhook_marks_reauthorization_as_permanent(monkeypatch):
    import httpx

    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        tenant_id="default",
        external_account_id="bot-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
            "xchat_private_keys_b64": "private",
        },
        config={},
    )
    payload = {
        "data": {
            "event_type": "chat.received",
            "payload": {
                "conversation_id": "bot-1:user-1",
                "sender_id": "user-1",
                "encoded_event": "cipher",
            },
        }
    }

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get_user_public_keys(self, user_id):
            request = httpx.Request("GET", "https://api.x.com/2/users/user-1/public_keys")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("unauthorized", request=request, response=response)

        async def aclose(self):
            pass

    statuses = []

    async def fake_account(value):
        return account

    async def fake_claim(*args):
        return xchat_webhook.XChatClaim(payload=payload, token="claim-3")

    async def fake_mark(raw_event_id, claim_token, status, **kwargs):
        assert claim_token == "claim-3"
        statuses.append(status)

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "_claim", fake_claim)
    monkeypatch.setattr(xchat_webhook, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_webhook, "_mark", fake_mark)

    await xchat_webhook.process_xchat_raw_event(uuid.uuid4(), account_id)
    assert statuses == ["XCHAT_REAUTHORIZATION_REQUIRED"]


async def test_xchat_webhook_stops_when_raw_event_is_already_claimed(monkeypatch):
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        tenant_id="default",
        external_account_id="bot-1",
    )

    async def fake_account(value):
        return account

    async def fake_claim(*args):
        return None

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "_claim", fake_claim)

    await xchat_webhook.process_xchat_raw_event(uuid.uuid4(), account_id)
