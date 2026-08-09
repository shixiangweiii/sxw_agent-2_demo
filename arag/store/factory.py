"""存储端口工厂：按 `*_BACKEND` 配置返回实现（mirror lippi-arag StoreFactory）。"""
from __future__ import annotations

from arag.config import AragSettings
from arag.store.fulltext_index import FullTextIndex, LocalBM25Index
from arag.store.graph_store import GraphStore, LocalGraphStore
from arag.store.vector_store import LocalVectorStore, VectorStore


def build_vector_store(settings: AragSettings) -> VectorStore:
    if settings.vector_backend == "local":
        return LocalVectorStore()
    # TODO: pgvector / milvus 适配
    raise ValueError(f"unsupported VECTOR_BACKEND={settings.vector_backend}")


def build_fulltext_index(settings: AragSettings) -> FullTextIndex:
    if settings.fulltext_backend == "local":
        return LocalBM25Index()
    # TODO: elasticsearch 适配
    raise ValueError(f"unsupported FULLTEXT_BACKEND={settings.fulltext_backend}")


def build_graph_store(settings: AragSettings) -> GraphStore:
    if settings.graph_backend == "local":
        return LocalGraphStore()
    # TODO: neo4j / nebula 适配
    raise ValueError(f"unsupported GRAPH_BACKEND={settings.graph_backend}")
