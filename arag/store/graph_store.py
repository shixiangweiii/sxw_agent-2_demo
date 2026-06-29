"""图存储端口 + 本地极简实现（GraphRAG 扩展点）。

按需求：本端口**仅注册、不接入默认检索流**；GraphRAG（实体抽取→建图→邻居扩展召回）待后续接 Neo4j。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from typing import Any


class GraphStore(ABC):
    """图存储端口。本地用内存邻接表；生产可换 Neo4j / NebulaGraph。"""

    @abstractmethod
    async def add_edge(self, src: str, dst: str, relation: str = "", **attrs: Any) -> None:
        ...

    @abstractmethod
    async def neighbors(self, node: str, depth: int = 1) -> list[str]:
        ...

    @abstractmethod
    async def clear(self) -> None:
        ...


class LocalGraphStore(GraphStore):
    """内存邻接表。仅作端口占位，当前未参与检索。"""

    def __init__(self) -> None:
        self._adj: dict[str, list[tuple[str, str]]] = {}

    async def add_edge(self, src: str, dst: str, relation: str = "", **attrs: Any) -> None:
        self._adj.setdefault(src, []).append((dst, relation))

    async def neighbors(self, node: str, depth: int = 1) -> list[str]:
        # 极简 BFS 邻居扩展。当前未接入检索流。
        # TODO: wire GraphRAG（实体抽取→建图→邻居扩展召回）when Neo4j backend ready.
        seen: set[str] = set()
        frontier: deque[tuple[str, int]] = deque([(node, 0)])
        while frontier:
            cur, d = frontier.popleft()
            if d >= depth:
                continue
            for dst, _rel in self._adj.get(cur, []):
                if dst not in seen:
                    seen.add(dst)
                    frontier.append((dst, d + 1))
        return list(seen)

    async def clear(self) -> None:
        self._adj = {}
