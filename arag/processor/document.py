"""文档处理：原始 markdown/text → Document，并提供图片提取（多模态接入点）。"""
from __future__ import annotations

import re
from typing import Any, Optional

from arag.store.base import Document

# markdown 图片语法 ![alt](url)；只认 http(s) 真实地址（对齐原项目 extract_image_urls_from_text）
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")


def to_document(
    doc_id: str,
    title: str,
    content: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Document:
    return Document(doc_id=doc_id, title=title, content=content, metadata=dict(metadata or {}))


def extract_image_urls(content: str) -> list[str]:
    """抽取正文里的 markdown 图片 URL（供 M5 图片多模态处理使用）。"""
    return _MD_IMAGE.findall(content)
