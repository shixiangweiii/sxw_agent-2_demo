"""native_loop 的会话历史存储：``(user_id, session_id) → list[Msg]``。

对应 CC ``QueryEngine`` 的 ``mutableMessages``——一个会话一份扁平数组，
每轮把本轮产生的消息追加进去。刻意不复用 ADK ``InMemorySessionService``：
那是 Event 结构，与本引擎的 Msg 模型不同构，转换成本高于自己存。

``ENGINE`` 是启动期配置，同进程只会有一代引擎在跑，因此不存在两套存储并存打架。
与 ADK 侧一样是**进程内内存**实现，重启即丢——生产可换 Redis / DB，业务只依赖本类接口。
"""
from __future__ import annotations

import logging
from typing import Optional

from agent.engine.native_loop.messages import Msg, clone
from common.obs import get_logger, log_kv

logger = get_logger("agent.native")


class HistoryStore:
    def __init__(self, max_messages_per_session: int = 400) -> None:
        self._sessions: dict[tuple[str, str], list[Msg]] = {}
        # 兜底上限：防止长会话在内存里无限增长。正常情况下 compact 会先把历史压下去，
        # 这里只是最后一道防线（触发时会告警，说明压缩没跟上）。
        self._max_messages = max_messages_per_session

    @staticmethod
    def _key(user_id: str, session_id: str) -> tuple[str, str]:
        return (user_id, session_id)

    def get(self, user_id: str, session_id: str) -> list[Msg]:
        """返回该会话历史的副本，供本轮循环自由改写而不影响已存内容。"""
        return clone(self._sessions.get(self._key(user_id, session_id), []))

    def replace(self, user_id: str, session_id: str, messages: list[Msg]) -> None:
        """用本轮结束后的完整消息列表覆盖会话历史。"""
        key = self._key(user_id, session_id)
        stored = clone(messages)
        if len(stored) > self._max_messages:
            dropped = len(stored) - self._max_messages
            stored = stored[dropped:]
            log_kv(logger, logging.WARNING, "History", "session truncated to cap",
                   user=user_id, session=session_id, dropped=dropped,
                   cap=self._max_messages)
        self._sessions[key] = stored

    def clear(self, user_id: str, session_id: str) -> None:
        self._sessions.pop(self._key(user_id, session_id), None)

    def size(self, user_id: str, session_id: str) -> Optional[int]:
        found = self._sessions.get(self._key(user_id, session_id))
        return None if found is None else len(found)
