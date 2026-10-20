"""A small in-memory vector store with cosine similarity. Swap for pgvector/Qdrant in production."""
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

    @staticmethod
    def _normalize(v: np.ndarray) -> np.ndarray:
        norms = np.linalg.norm(v, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return v / norms

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors must have the same length")
        if not chunks:
            return
        vectors = self._normalize(vectors.astype(np.float32))
        with self._lock:
            self._chunks.extend(chunks)
            self._vectors = vectors if self._vectors is None else np.vstack([self._vectors, vectors])

    def search(self, query_vector: np.ndarray, k: int, doc_ids: list[str] | None = None) -> list[Hit]:
        with self._lock:
            if self._vectors is None:
                return []
            q = self._normalize(query_vector.reshape(1, -1))[0]
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

    def delete(self, doc_id: str) -> int:
        with self._lock:
            keep = [i for i, c in enumerate(self._chunks) if c.doc_id != doc_id]
            removed = len(self._chunks) - len(keep)
            self._chunks = [self._chunks[i] for i in keep]
            self._vectors = self._vectors[keep] if self._vectors is not None and keep else None
            return removed

    def documents(self) -> list[dict]:
        with self._lock:
            docs: dict[str, dict] = {}
            for c in self._chunks:
                d = docs.setdefault(c.doc_id, {"id": c.doc_id, "filename": c.filename, "chunks": 0, "pages": 0})
                d["chunks"] += 1
                d["pages"] = max(d["pages"], c.page)
            return list(docs.values())
