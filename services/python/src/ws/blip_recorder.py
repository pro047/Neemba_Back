"""Isolation layer between the ws hub and the ``app.ws_blips`` table (§4-7).

The hub calls these from fire-and-forget tasks; a DB failure must never reach
the /ws hot path (same isolation policy as the pusher capture path), so both
methods swallow every exception and degrade to "blip not recorded".
"""
from __future__ import annotations

from src.repository.implementation import ws_blip_repository as wb


class WsBlipRecorder:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def record_disconnect(
        self,
        session_id: str,
        *,
        close_code: int | None = None,
        close_reason: str | None = None,
        detected_by: str,
    ) -> int | None:
        """Insert an open blip row; return its id (None if the write failed)."""
        try:
            return await wb.insert_blip(
                self._pool,
                session_id=session_id,
                close_code=close_code,
                close_reason=close_reason,
                detected_by=detected_by,
            )
        except Exception as e:
            print("blips: record_disconnect failed (ignored):", repr(e))
            return None

    async def record_reconnect(
        self, blip_id: int, *, flushed_count: int, lost_count: int
    ) -> None:
        """Close an open blip (reconnect timestamp, duration, counts)."""
        try:
            await wb.close_blip(
                self._pool,
                blip_id=blip_id,
                flushed_count=flushed_count,
                lost_count=lost_count,
            )
        except Exception as e:
            print("blips: record_reconnect failed (ignored):", repr(e))
