"""存储端口层（依赖倒置）：VectorStore / FullTextIndex / GraphStore + 工厂。

Document/chunk 权威数据只存于 ``rag.db``；本地 numpy/BM25 是可重建的进程内投影，
GraphStore 仍为内存占位。端口保留了未来替换检索后端的边界，但不形成第二事实源。
"""
