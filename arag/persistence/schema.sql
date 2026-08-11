-- The single current ARAG schema.  This project does not migrate databases:
-- a mismatching local database must be deleted and recreated by the operator.
CREATE TABLE schema_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version TEXT NOT NULL,
    schema_checksum TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE documents (
    document_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    external_doc_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(dataset_id, external_doc_id)
);

CREATE TABLE document_versions (
    version_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    content_digest TEXT NOT NULL,
    content_uri TEXT NOT NULL,
    title TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(document_id, content_digest)
);

CREATE TABLE active_document_versions (
    document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    activated_at INTEGER NOT NULL
);

CREATE TABLE chunks (
    chunk_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    ordinal INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    page INTEGER,
    span_start INTEGER NOT NULL,
    span_end INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    UNIQUE(version_id, ordinal)
);

CREATE TABLE index_jobs (
    job_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    version_id TEXT NOT NULL REFERENCES document_versions(version_id),
    state TEXT NOT NULL CHECK(state IN ('PREPARED','BUILDING','VALIDATING','READY','ACTIVATED','FAILED')),
    attempt INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_message TEXT,
    expected_chunk_count INTEGER,
    projection_digest TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(version_id)
);

CREATE INDEX idx_index_jobs_state_updated ON index_jobs(state, updated_at, job_id);

CREATE TABLE chunk_embeddings (
    chunk_id TEXT PRIMARY KEY REFERENCES chunks(chunk_id) ON DELETE CASCADE,
    embedding_model TEXT NOT NULL,
    dim INTEGER NOT NULL CHECK(dim > 0),
    vector_blob BLOB NOT NULL,
    checksum TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE projection_metadata (
    projection_name TEXT PRIMARY KEY,
    generation INTEGER NOT NULL,
    source_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TRIGGER chunks_no_update
BEFORE UPDATE ON chunks BEGIN
    SELECT RAISE(ABORT, 'chunks are immutable');
END;
