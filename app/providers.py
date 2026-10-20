"""Model providers. OpenAI for real use; a deterministic fake for offline demos and tests."""
from __future__ import annotations

import hashlib
import re
from typing import Iterator, Protocol

import numpy as np


class Provider(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...
    def stream_chat(self, system: str, user: str) -> Iterator[str]: ...


class OpenAIProvider:
    def __init__(self, api_key: str, chat_model: str, embed_model: str):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key)
        self.chat_model = chat_model
        self.embed_model = embed_model

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for i in range(0, len(texts), 96):  # batch to stay under request limits
            resp = self.client.embeddings.create(model=self.embed_model, input=texts[i : i + 96])
            vectors.extend(d.embedding for d in resp.data)
        return np.asarray(vectors, dtype=np.float32)

    def stream_chat(self, system: str, user: str) -> Iterator[str]:
        stream = self.client.chat.completions.create(
            model=self.chat_model,
            temperature=0.1,
            stream=True,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        for event in stream:
            delta = event.choices[0].delta.content if event.choices else None
            if delta:
                yield delta


class FakeProvider:
    """Hashed bag-of-words embeddings and an extractive 'answer'. No network, fully deterministic."""

    dim = 512

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                out[row, h % self.dim] += 1.0
        return out

    def stream_chat(self, system: str, user: str) -> Iterator[str]:
        first_source = re.search(r"\[1\][^\n]*\n(.+?)(?:\n\[2\]|\nQuestion:)", user, re.S)
        snippet = first_source.group(1).strip()[:300] if first_source else "I couldn't find that in your documents."
        answer = f"(offline demo mode) Based on your documents: {snippet} [1]"
        for word in answer.split(" "):
            yield word + " "


def make_provider(settings) -> Provider:
    if settings.provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIProvider(settings.openai_api_key, settings.chat_model, settings.embed_model)
    return FakeProvider()
