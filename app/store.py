"""A small in-memory vector store with cosine similarity. Swap for pgvector/Qdrant in production.

Memory note: vectors are kept as normalised float32 (768 dims = 3 KB per chunk)."""
from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from .ingest import Chunk


@dataclass
class Hit:
    chunk: Chunk
    score: float


class VectorStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None
        self._meta: dict[str, dict] = {}  # doc_id -> {filename, pages, meta}

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return v / norms

    @property
    def dim(self) -> int | None:
        return None if self._vectors is None else int(self._vectors.shape[1])

    def add(self, chunks: list[Chunk], vectors: np.ndarray, pages: int | None = None, meta: dict | None = None) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        if not chunks:
            return
        vectors = self._normalize(np.asarray(vectors, dtype=np.float32))
        with self._lock:
            if self._vectors is not None and self._vectors.shape[1] != vectors.shape[1]:
                raise ValueError("vector dimension does not match this workspace")
            self._chunks.extend(chunks)
            self._vectors = vectors if self._vectors is None else np.vstack([self._vectors, vectors])
            for c in chunks:
                m = self._meta.setdefault(c.doc_id, {"filename": c.filename, "pages": 0, "meta": {}})
                m["pages"] = max(m["pages"], c.page)
            first = chunks[0].doc_id
            if pages:
                self._meta[first]["pages"] = max(self._meta[first]["pages"], int(pages))
            if meta:
                self._meta[first]["meta"] = dict(meta)

    def search(self, query_vector: np.ndarray, k: int, doc_ids: list[str] | None = None) -> list[Hit]:
        with self._lock:
            if self._vectors is None:
                return []
            q = np.asarray(query_vector, dtype=np.float32).reshape(1, -1)
            if q.shape[1] != self._vectors.shape[1]:
                return []
            q = self._normalize(q)[0]
            scores = self._vectors @ q
            order = np.argsort(-scores)
            hits = []
            for idx in order:
                chunk = self._chunks[idx]
                if doc_ids and chunk.doc_id not in doc_ids:
                    continue
                hits.append(Hit(chunk=chunk, score=float(scores[idx])))
                if len(hits) == k:
                    break
            return hits

    def doc_chunks(self, doc_id: str) -> list[Chunk]:
        """All chunks of one document in reading order."""
        with self._lock:
            return [c for c in self._chunks if c.doc_id == doc_id]

    def has(self, doc_id: str) -> bool:
        with self._lock:
            return doc_id in self._meta

    def info(self, doc_id: str) -> dict | None:
        with self._lock:
            m = self._meta.get(doc_id)
            return None if m is None else {"id": doc_id, **m}

    def delete(self, doc_id: str) -> int:
        with self._lock:
            keep = [i for i, c in enumerate(self._chunks) if c.doc_id != doc_id]
            removed = len(self._chunks) - len(keep)
            self._chunks = [self._chunks[i] for i in keep]
            self._vectors = self._vectors[keep] if self._vectors is not None and keep else None
            self._meta.pop(doc_id, None)
            return removed

    def documents(self) -> list[dict]:
        with self._lock:
            counts: dict[str, int] = {}
            for c in self._chunks:
                counts[c.doc_id] = counts.get(c.doc_id, 0) + 1
            return [
                {"id": doc_id, "filename": m["filename"], "chunks": counts.get(doc_id, 0), "pages": m["pages"]}
                for doc_id, m in self._meta.items()
            ]
