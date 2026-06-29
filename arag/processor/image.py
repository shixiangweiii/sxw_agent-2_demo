"""图片多模态处理：文档内 markdown 图片 → 视觉模型生成 caption，并入正文使其可检索。

对应原项目 processor/image（OCR / LLM caption）。这里用 qwen3.7-plus 视觉做 caption。
"""
from __future__ import annotations

import logging

from arag.components.llm import ChatClient
from arag.processor.document import extract_image_urls
from arag.store.base import Document
from common.obs import get_logger, log_kv

logger = get_logger("arag.image")

_CAPTION_PROMPT = "用一句话简洁描述这张图片的主要内容（中文）。"


class ImageProcessor:
    def __init__(self, chat: ChatClient) -> None:
        self._chat = chat

    async def enrich(self, doc: Document) -> Document:
        """为文档内每张图片生成 caption 并追加到正文（可被全文/向量检索命中）。"""
        urls = extract_image_urls(doc.content)
        if not urls:
            return doc
        captions: list[str] = []
        for url in urls:
            try:
                caption = await self._chat.vision(_CAPTION_PROMPT, url)
            except Exception as exc:  # noqa: BLE001 - 单图失败不应阻断入库
                log_kv(logger, logging.WARNING, "ImageCaption", "caption failed",
                       error=type(exc).__name__)
                continue
            if caption.strip():
                captions.append(f"图片描述：{caption.strip()}")
        if not captions:
            return doc
        log_kv(logger, logging.INFO, "ImageCaption", "captioned",
               doc_id=doc.doc_id, count=len(captions))
        return Document(
            doc_id=doc.doc_id,
            title=doc.title,
            content=doc.content + "\n\n" + "\n".join(captions),
            metadata=doc.metadata,
        )
