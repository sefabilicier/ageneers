"""
VectorIndex — semantic file search using ChromaDB + sentence-transformers.

How it works:
    1. INDEX phase  : read source files, split into chunks, embed with
                      sentence-transformers (all-MiniLM-L6-v2, ~80MB, runs locally,
                      no API key), store vectors in an in-memory ChromaDB collection.
    2. QUERY phase  : embed the task requirement with the same model,
                      run cosine-similarity search, return the top-k most
                      semantically relevant file paths.

Why this is better than LLM path ranking:
    - Deterministic and fast (no LLM call, no rate limit).
    - Works at file-content level, not just file names.
    - Scales to large repos — we chunk files and search chunks, then
      deduplicate back to file paths.
    - No tokens consumed.

Design choices:
    - In-memory ChromaDB collection (no disk persistence needed — the index
      is rebuilt per pipeline run from the fresh clone).
    - Chunk size: 512 chars with 64-char overlap — small enough that
      semantically related code stays together, large enough to carry context.
    - Model: all-MiniLM-L6-v2 (384-dim, very fast, good code/text alignment).
    - Fallback: if chromadb or sentence-transformers are not installed,
      raises ImportError with a clear message so the caller can fall back
      to the LLM ranking path.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

CHUNK_SIZE    = 512    # chars per chunk
CHUNK_OVERLAP = 64     # overlap between consecutive chunks
DEFAULT_TOP_K = 10     # number of files to return


# ─────────────────────────────────────────────────────────────────────────────
# Chunker
# ─────────────────────────────────────────────────────────────────────────────

def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks of `size` characters."""
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start += size - overlap
    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# VectorIndex
# ─────────────────────────────────────────────────────────────────────────────

class VectorIndex:
    """
    In-memory vector index for a single repository.

    Usage:
        index = VectorIndex()
        index.build(workspace_path, source_files)
        top_files = index.query(requirement, top_k=10)
    """

    def __init__(self) -> None:
        self._collection: Any = None
        self._ef: Any = None       # embedding function

    def _load_deps(self) -> None:
        """Lazy-load chromadb and sentence-transformers."""
        if self._collection is not None:
            return

        try:
            import chromadb
            from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
        except ImportError as exc:
            raise ImportError(
                "chromadb and sentence-transformers are required for vector indexing. "
                "Install them with: pip install chromadb sentence-transformers"
            ) from exc

        self._ef = SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )

        client = chromadb.Client()   # in-memory, no persistence
        # Use a fresh collection name each time to avoid state leaks
        self._collection = client.create_collection(
            name="repo_index",
            embedding_function=self._ef,
            metadata={"hnsw:space": "cosine"},
        )
        logger.info("vector_index.initialized", model="all-MiniLM-L6-v2")

    def build(self, workspace: Path, source_files: list[str]) -> int:
        """
        Read source files, chunk them, and embed into the collection.

        Args:
            workspace    : absolute path to the cloned repo root
            source_files : list of relative file paths to index

        Returns:
            Number of chunks indexed.
        """
        self._load_deps()

        documents: list[str] = []
        metadatas: list[dict] = []
        ids:       list[str] = []
        chunk_idx = 0

        for rel_path in source_files:
            full = workspace / rel_path
            if not full.exists() or not full.is_file():
                continue
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            chunks = _chunk_text(content)
            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                documents.append(chunk)
                metadatas.append({"file_path": rel_path, "chunk_index": i})
                ids.append(f"chunk-{chunk_idx}")
                chunk_idx += 1

        if documents:
            # ChromaDB recommends batches of ≤5000
            batch = 500
            for start in range(0, len(documents), batch):
                self._collection.add(
                    documents=documents[start:start + batch],
                    metadatas=metadatas[start:start + batch],
                    ids=ids[start:start + batch],
                )

        logger.info("vector_index.built",
                    files=len(source_files), chunks=chunk_idx)
        return chunk_idx

    def query(self, requirement: str, top_k: int = DEFAULT_TOP_K) -> list[str]:
        """
        Find the top-k most semantically relevant files for a requirement.

        Args:
            requirement : the task requirement text (natural language)
            top_k       : number of distinct file paths to return

        Returns:
            Ordered list of relative file paths (most relevant first).
        """
        if self._collection is None:
            raise RuntimeError("VectorIndex.build() must be called before query()")

        # Ask for more results than needed so deduplication leaves enough
        n_results = min(top_k * 5, self._collection.count())
        if n_results == 0:
            return []

        results = self._collection.query(
            query_texts=[requirement],
            n_results=n_results,
        )

        # Deduplicate: each file path appears once, keeping its best-ranked chunk
        seen: set[str] = set()
        ranked_files: list[str] = []
        for meta in results["metadatas"][0]:
            fp = meta["file_path"]
            if fp not in seen:
                seen.add(fp)
                ranked_files.append(fp)
            if len(ranked_files) >= top_k:
                break

        logger.info("vector_index.query_result",
                    requirement=requirement[:60],
                    top_files=ranked_files)
        return ranked_files