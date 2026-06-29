"""低价值过滤：去重 + 去空/过短 + 分数阈值。"""
from __future__ import annotations

from dataclasses import dataclass

from arag.store.base import Chunk


@dataclass
class FilterConfig:
    min_chars: int = 2
    min_score: float = 0.0


def low_value_filter(chunks: list[Chunk], cfg: FilterConfig | None = None) -> list[Chunk]:
    cfg = cfg or FilterConfig()
    out: list[Chunk] = []
    seen: set[str] = set()
    for c in chunks:
        if len(c.content.strip()) < cfg.min_chars:
            continue
        if c.score < cfg.min_score:
            continue
        if c.chunk_id in seen:
            continue
        seen.add(c.chunk_id)
        out.append(c)
    return out
