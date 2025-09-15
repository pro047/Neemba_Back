
from deepl import TextResult
from src.ws.websocket import WebSocketHub


class Pusher:
    def __init__(self, hub: WebSocketHub) -> None:
        self.hub = hub

    async def push_to_client(self, push_text: TextResult | list[TextResult],  sequence: int | None):
        await self.hub.broadcast_to_session(payload={
            "sequence": sequence,
            "sentence": push_text,
            "isFinal": True
        })
