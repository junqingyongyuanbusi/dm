import uuid
from types import SimpleNamespace

from social_reply.application.event_ingestion import xchat_webhook


async def test_xchat_webhook_marks_pin_required(monkeypatch):
    raw_id = uuid.uuid4()
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
        external_account_id="bot-1",
        credential_bundle={
            "consumer_key": "ck",
            "consumer_secret": "cs",
            "access_token": "at",
            "access_token_secret": "ats",
        },
        config={},
    )
    raw = SimpleNamespace(
        payload={
            "data": {
                "event_type": "chat.received",
                "event_uuid": "event-1",
                "payload": {"sender_id": "user-1", "encoded_event": "cipher"},
            }
        }
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return raw

    async def fake_account(value):
        return account

    statuses = []

    async def fake_mark(value, status):
        statuses.append(status)

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(xchat_webhook, "_mark", fake_mark)

    await xchat_webhook.process_xchat_raw_event(raw_id, account_id)
    assert statuses == ["XCHAT_PIN_REQUIRED"]


async def test_xchat_webhook_decrypts_and_ingests(monkeypatch):
    raw_id = uuid.uuid4()
    account_id = uuid.uuid4()
    account = SimpleNamespace(
        id=account_id,
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
    raw = SimpleNamespace(
        payload={
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
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, model, key):
            return raw

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def get_user_public_keys(self, user_id):
            return []

        async def aclose(self):
            pass

    async def fake_account(value):
        return account

    def fake_decrypt(**kwargs):
        return {
            "type": "Message",
            "message_id": "message-1",
            "sender_id": "user-1",
            "content": {"content_type": "Text", "text": "hello"},
            "verified": True,
        }

    ingested = []

    async def fake_ingest(event, *, raw_event_id):
        ingested.append((event, raw_event_id))
        return uuid.uuid4()

    monkeypatch.setattr(xchat_webhook, "get_platform_account_runtime", fake_account)
    monkeypatch.setattr(xchat_webhook, "get_session_factory", lambda: lambda: FakeSession())
    monkeypatch.setattr(xchat_webhook, "XChatClient", FakeClient)
    monkeypatch.setattr(xchat_webhook, "decrypt_live_event", fake_decrypt)
    monkeypatch.setattr(xchat_webhook, "ingest_canonical_event", fake_ingest)

    await xchat_webhook.process_xchat_raw_event(raw_id, account_id)
    assert ingested[0][0].external_event_id == "message-1"
    assert ingested[0][1] == raw_id
