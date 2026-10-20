"""Retrieval-augmented answering with numbered, page-level citations."""
from __future__ import annotations

from typing import Iterator

from .store import Hit, VectorStore

SYSTEM_PROMPT = """You are a precise assistant that answers ONLY from the provided sources.
Rules:
- Cite every claim with the source number in square brackets, e.g. [1] or [2][3].
- If the sources don't contain the answer, say "I couldn't find that in your documents." Do not guess.
- Be concise. Use short paragraphs or bullet points."""


def build_prompt(question: str, hits: list[Hit]) -> str:
    blocks = [
        f"[{i}] {h.chunk.filename}, page {h.chunk.page}\n{h.chunk.text}" for i, h in enumerate(hits, start=1)
    ]
    return "Sources:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"


def citations(hits: list[Hit]) -> list[dict]:
    return [
        {"n": i, "filename": h.chunk.filename, "page": h.chunk.page, "score": round(h.score, 3),
         "snippet": h.chunk.text[:240]}
        for i, h in enumerate(hits, start=1)
    ]


class RagEngine:
    def __init__(self, provider, store: VectorStore, top_k: int, min_score: float):
        self.provider = provider
        self.store = store
        self.top_k = top_k
        self.min_score = min_score

    def retrieve(self, question: str, doc_ids: list[str] | None = None) -> list[Hit]:
        q = self.provider.embed([question])[0]
        hits = self.store.search(q, self.top_k, doc_ids)
        return [h for h in hits if h.score >= self.min_score]

    def stream_answer(self, question: str, hits: list[Hit]) -> Iterator[str]:
        if not hits:
            yield "I couldn't find that in your documents."
            return
        yield from self.provider.stream_chat(SYSTEM_PROMPT, build_prompt(question, hits))
