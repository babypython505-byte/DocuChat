"""Retrieval-augmented answering with numbered, page-level citations and short multi-turn memory."""
from __future__ import annotations

from typing import Iterator

import numpy as np

from .store import Hit, VectorStore

SYSTEM_PROMPT = """You are DocuChat, a precise study and research assistant that answers ONLY from the provided sources.
Rules:
- Every factual claim must come from the numbered sources and cite them in square brackets, e.g. [1] or [2][3].
- Never use outside knowledge for factual claims. If the sources don't contain the answer, say "I couldn't find that in your documents." and, if useful, say what the documents do cover. Do not guess.
- The conversation history only tells you what the user is referring to; it is not a source.
- Be clear and concise. Use Markdown: short paragraphs, bullet points, tables when comparing, and LaTeX ($...$ or $$...$$) for math."""

NOT_FOUND = "I couldn't find that in your documents."
MAX_HISTORY_MESSAGES = 6
MAX_HISTORY_CHARS = 1200  # per message


def trim_history(history: list[dict] | None) -> list[dict]:
    """Keep the last few user/assistant turns, each capped, so prompts stay small."""
    out = []
    for m in (history or [])[-MAX_HISTORY_MESSAGES:]:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role in ("user", "assistant") and content:
            text = str(content).strip()
            if len(text) > MAX_HISTORY_CHARS:
                text = text[:MAX_HISTORY_CHARS] + " …"
            out.append({"role": role, "content": text})
    return out


def condense_query(question: str, history: list[dict] | None) -> str:
    """Retrieval query for a follow-up: the previous user question plus this one, so "what about its limits?"
    still retrieves passages about the earlier topic."""
    prev = [m["content"] for m in trim_history(history) if m["role"] == "user"]
    if not prev:
        return question
    return f"{prev[-1][:500]}\n{question}"


def format_history(history: list[dict] | None) -> str:
    turns = trim_history(history)
    if not turns:
        return ""
    lines = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in turns]
    return "Conversation so far (context only, not a source):\n" + "\n".join(lines) + "\n\n"


def build_prompt(question: str, hits: list[Hit], history: list[dict] | None = None) -> str:
    blocks = [
        f"[{i}] {h.chunk.filename}, page {h.chunk.page}\n{h.chunk.text}" for i, h in enumerate(hits, start=1)
    ]
    return format_history(history) + "Sources:\n\n" + "\n\n".join(blocks) + f"\n\nQuestion: {question}"


def citations(hits: list[Hit]) -> list[dict]:
    return [
        {"n": i, "doc_id": h.chunk.doc_id, "filename": h.chunk.filename, "page": h.chunk.page,
         "score": round(h.score, 3), "snippet": h.chunk.text[:240], "text": h.chunk.text[:1200]}
        for i, h in enumerate(hits, start=1)
    ]


def embed_queries(provider, texts: list[str]) -> np.ndarray:
    many = getattr(provider, "embed_queries", None)
    if many:
        return many(texts)
    one = getattr(provider, "embed_query", None)
    if one:
        return np.vstack([one(t) for t in texts])
    return provider.embed(texts)


class RagEngine:
    def __init__(self, provider, store: VectorStore, top_k: int, min_score: float):
        self.provider = provider
        self.store = store
        self.top_k = top_k
        self.min_score = min_score

    def retrieve(self, question: str, doc_ids: list[str] | None = None, history: list[dict] | None = None) -> list[Hit]:
        query = condense_query(question, history)
        q = embed_queries(self.provider, [query])[0]
        hits = self.store.search(q, self.top_k, doc_ids)
        return [h for h in hits if h.score >= self.min_score]

    def stream_answer(self, question: str, hits: list[Hit], history: list[dict] | None = None) -> Iterator[str]:
        if not hits:
            yield NOT_FOUND
            return
        yield from self.provider.stream_chat(SYSTEM_PROMPT, build_prompt(question, hits, history))
