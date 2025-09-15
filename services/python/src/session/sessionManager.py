import asyncio
from typing import Optional


class SessionManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._current_session_id: Optional[str] = None

    async def set_session(self, session_id: str) -> None:
        async with self._lock:
            self._current_session_id = session_id

    async def get_session(self) -> str:
        async with self._lock:
            if self._current_session_id is None:
                raise RuntimeError("Session Id not set yet")
            return self._current_session_id
