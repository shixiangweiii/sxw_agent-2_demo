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
from arag.persistence.repository import RagRepository
from arag.persistence.service import IndexCoordinator
from arag.projection.snapshot import ProjectionManager
from arag.store.factory import build_graph_store
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
    repository: RagRepository
    projections: ProjectionManager
    index_coordinator: IndexCoordinator


async def build_context(settings: AragSettings) -> AragContext:
    repository = RagRepository(settings.rag_db_path, settings.rag_storage_dir)
    await repository.initialize()
    projections = ProjectionManager(repository)
    await projections.rebuild()
    vector_store = projections.vector_store
    fulltext_index = projections.fulltext_index
    graph_store = build_graph_store(settings)
    embedder = Embedder(settings)
    chat = ChatClient(settings)
    chunker = Chunker()
    rewriter = QueryRewriter(chat)
    retriever = HybridRetriever(vector_store, fulltext_index, embedder, rewriter)
    generator = Generator(chat)
    image_processor = ImageProcessor(chat)
    index_coordinator = IndexCoordinator(
        repository=repository,
        projections=projections,
        enricher=image_processor,
        chunker=chunker,
        embedder=embedder,
        embedding_model=settings.embedding_model,
        poll_interval_seconds=settings.index_job_poll_interval_seconds,
    )
    await index_coordinator.start()
    log_kv(
        logger,
        logging.INFO,
        "Boot",
        "rebuilt search projections from rag.db authority",
        chunks=len(projections.snapshot.chunks),
        projection_state=await repository.projection_state(),
    )
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
        repository=repository,
        projections=projections,
        index_coordinator=index_coordinator,
    )


async def close_context(ctx: AragContext) -> None:
    await ctx.index_coordinator.stop()
