import httpx

from social_reply.connectors.x.client import XClient


async def test_x_client_returns_next_page_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["pagination_token"] == "page-2"
        return httpx.Response(
            200,
            json={"data": [{"id": "2"}], "meta": {"next_token": "page-3"}},
        )

    client = XClient(
        consumer_key="ck",
        consumer_secret="cs",
        access_token="at",
        access_token_secret="ats",
        transport=httpx.MockTransport(handler),
    )
    try:
        events, token = await client.read_dm_events(pagination_token="page-2")
    finally:
        await client.aclose()
    assert events == [{"id": "2"}]
    assert token == "page-3"
