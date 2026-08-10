"""
In-process WebSocket connection registry for real-time chat: message push,
typing indicators, and online presence.

This is deliberately in-memory (a plain dict on the running process), which
is the right amount of complexity for a single-instance deployment. If this
API is ever run behind multiple worker processes/machines, presence and
broadcast would need to move to a shared layer (e.g. Redis pub/sub) since
each process would otherwise only know about its own connections.
"""

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self._connections: dict[int, list[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: int, websocket: WebSocket) -> bool:
        """Accepts the socket and registers it. Returns True if this is the
        user's first open connection (i.e. they just came online)."""
        await websocket.accept()
        async with self._lock:
            was_offline = user_id not in self._connections
            self._connections.setdefault(user_id, []).append(websocket)
        return was_offline

    async def disconnect(self, user_id: int, websocket: WebSocket) -> bool:
        """Unregisters the socket. Returns True if the user has no other
        open connections left (i.e. they just went offline)."""
        async with self._lock:
            conns = self._connections.get(user_id)
            if not conns:
                return True
            if websocket in conns:
                conns.remove(websocket)
            if conns:
                return False
            del self._connections[user_id]
            return True

    def is_online(self, user_id: int) -> bool:
        return bool(self._connections.get(user_id))

    def online_user_ids(self, user_ids) -> set[int]:
        return {uid for uid in user_ids if self.is_online(uid)}

    async def send_to_user(self, user_id: int, payload: dict) -> None:
        conns = list(self._connections.get(user_id, []))
        for ws in conns:
            try:
                await ws.send_json(payload)
            except Exception:
                logger.info("Dropping dead websocket for user %s", user_id)
                await self.disconnect(user_id, ws)

    async def send_to_users(self, user_ids, payload: dict, exclude: int | None = None) -> None:
        for uid in user_ids:
            if uid == exclude:
                continue
            await self.send_to_user(uid, payload)


# One shared instance for the whole process — imported by chat_routes.
manager = ConnectionManager()
