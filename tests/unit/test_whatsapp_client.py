import httpx

from social_reply.connectors.whatsapp.client import WhatsAppClient


async def test_whatsapp_client_validates_and_sends_session_text():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"id": "phone-1", "verified_name": "Support", "quality_rating": "GREEN"},
            )
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})

    client = WhatsAppClient(
        access_token="token",
        phone_number_id="phone-1",
        transport=httpx.MockTransport(handler),
    )
    profile = await client.get_phone_number()
    message_id = await client.send_text(
        target={"kind": "session_message", "to": "15551234567"}, text="Hello"
    )
    assert profile["verified_name"] == "Support"
    assert message_id == "wamid.1"
    assert requests[1].url.path.endswith("/phone-1/messages")
    assert b'"messaging_product":"whatsapp"' in requests[1].content
    await client.aclose()
