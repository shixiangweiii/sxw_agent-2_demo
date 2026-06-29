"""存储层通用数据类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    """入库文档（解析前的逻辑单元）。"""

    doc_id: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Chunk:
    """检索最小单元（切分后的片段 + 命中分数）。"""

    chunk_id: str
    doc_id: str
    content: str
    title: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
