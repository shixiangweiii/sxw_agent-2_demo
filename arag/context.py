"""arag 运行时上下文：装配全部组件（依赖注入容器）。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from arag.components.chunker import Chunker
from arag.components.embedding import Embedder
from arag.components.generator import Generator
from arag.components.llm import ChatClient
from arag.components.retriever import HybridRetriever
from arag.components.rewrite import QueryRewriter
from arag.config import AragSettings
from arag.processor.image import ImageProcessor
from arag.store.factory import build_fulltext_index, build_graph_store, build_vector_store
from arag.store.fulltext_index import FullTextIndex
from arag.store.graph_store import GraphStore
from arag.store.vector_store import VectorStore
from common.obs import get_logger, log_kv

logger = get_logger("arag.context")


@dataclass
class AragContext:
    settings: AragSettings
    vector_store: VectorStore
    fulltext_index: FullTextIndex
    graph_store: GraphStore           # 仅注册，未接检索流
    embedder: Embedder
    chat: ChatClient
    chunker: Chunker
    rewriter: QueryRewriter
    retriever: HybridRetriever
    generator: Generator
    image_processor: ImageProcessor


async def build_context(settings: AragSettings) -> AragContext:
    vector_store = build_vector_store(settings)
    fulltext_index = build_fulltext_index(settings)
    graph_store = build_graph_store(settings)
    persisted_chunks = await vector_store.all_chunks()
    if persisted_chunks:
        await fulltext_index.add(persisted_chunks)
        log_kv(
            logger,
            logging.INFO,
            "Boot",
            "rebuilt fulltext index from persisted embeddings",
            chunks=len(persisted_chunks),
        )
    embedder = Embedder(settings)
    chat = ChatClient(settings)
    chunker = Chunker()
    rewriter = QueryRewriter(chat)
    retriever = HybridRetriever(vector_store, fulltext_index, embedder, rewriter)
    generator = Generator(chat)
    image_processor = ImageProcessor(chat)
    return AragContext(
        settings=settings,
        vector_store=vector_store,
        fulltext_index=fulltext_index,
        graph_store=graph_store,
        embedder=embedder,
        chat=chat,
        chunker=chunker,
        rewriter=rewriter,
        retriever=retriever,
        generator=generator,
        image_processor=image_processor,
    )
