"""SessionManager：封装 ADK InMemorySessionService，提供 get-or-create 语义。

生产可换 DatabaseSessionService / 自研 Redis 实现——业务只依赖此封装。
"""
from __future__ import annotations

from google.adk.sessions import InMemorySessionService
from google.adk.sessions.session import Session

# Runner 与 SessionService 必须用同一 app_name
APP_NAME = "sxw-agent"


class SessionManager:
    def __init__(self) -> None:
        self._svc = InMemorySessionService()

    @property
    def service(self) -> InMemorySessionService:
        return self._svc

    async def get_or_create(self, user_id: str, session_id: str) -> Session:
        session = await self._svc.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id,
        )
        if session is None:
            session = await self._svc.create_session(
                app_name=APP_NAME, user_id=user_id, session_id=session_id,
            )
        return session
