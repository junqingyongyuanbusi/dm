from typing import Protocol


class PlatformSender(Protocol):
    platform: str

    async def send_text(self, *, target: dict, text: str) -> str: ...

    async def aclose(self) -> None: ...
