"""arag 服务配置（pydantic-settings，env 驱动）。"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class AragSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM / embeddings (DashScope, OpenAI-compatible) ---
    dashscope_api_key: str = "sk-***"
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_model: str = "qwen3.7-plus"
    embedding_model: str = "text-embedding-v3"

    arag_port: int = 8100

    # --- Storage backends (ports) ---
    vector_backend: str = "local"         # local | pgvector ...
    fulltext_backend: str = "local"       # local | es ...
    graph_backend: str = "local"          # local | neo4j ...（仅端口，未接检索流）
    # SQLite is document/version/chunk authority.  In-memory vector/BM25 are rebuildable projections.
    rag_db_path: str = "local_storage/arag/rag.db"
    rag_storage_dir: str = "local_storage/arag"
    index_job_poll_interval_seconds: float = 0.25

    log_level: str = "INFO"


@lru_cache
def get_settings() -> AragSettings:
    return AragSettings()
