"""Model providers: Gemini (free tier), OpenAI, and a deterministic fake for offline demos and tests.

Every provider offers:
  embed(texts) -> (n, dim) float32      document embeddings
  embed_query(text) -> (dim,) float32   query embedding (optional; falls back to embed)
  complete(system, user, json_mode, max_tokens) -> str   one non-streaming completion
  stream_chat(system, user) -> Iterator[str]               chat answer (may be a single chunk)
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Iterator, Protocol

import numpy as np


class Provider(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...
    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 1024) -> str: ...
    def stream_chat(self, system: str, user: str) -> Iterator[str]: ...


class OpenAIProvider:
    dim = 1536

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

    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 1024) -> str:
        kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = self.client.chat.completions.create(
            model=self.chat_model, temperature=0.2, max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs,
        )
        return resp.choices[0].message.content or ""

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


class GeminiProvider:
    """Google Gemini via REST (free tier friendly). Embeddings: gemini-embedding-001 at 768 dims."""

    base = "https://generativelanguage.googleapis.com/v1beta"
    FALLBACK_CHAT_MODELS = ("gemini-2.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash", "gemini-flash-latest")

    def __init__(self, api_key: str, chat_model: str, embed_model: str, dim: int = 768):
        import httpx

        self.http = httpx.Client(timeout=60, headers={"x-goog-api-key": api_key})
        self.chat_model = chat_model
        self.embed_model = embed_model
        self.dim = dim

    def _embed(self, texts: list[str], task: str) -> np.ndarray:
        vectors = []
        for i in range(0, len(texts), 100):  # API batch limit
            body = {"requests": [
                {"model": f"models/{self.embed_model}", "content": {"parts": [{"text": t}]},
                 "taskType": task, "outputDimensionality": self.dim}
                for t in texts[i : i + 100]
            ]}
            r = self.http.post(f"{self.base}/models/{self.embed_model}:batchEmbedContents", json=body)
            r.raise_for_status()
            vectors.extend(e["values"] for e in r.json()["embeddings"])
        return np.asarray(vectors, dtype=np.float32)

    def embed(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def embed_queries(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, "RETRIEVAL_QUERY")

    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 1024) -> str:
        """Non-streaming generateContent (more reliable on the free tier).
        Tries the configured model first, then fast fallbacks; remembers the one that works."""
        config: dict = {"temperature": 0.2 if json_mode else 0.1, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        candidates = [self.chat_model] + [m for m in self.FALLBACK_CHAT_MODELS if m != self.chat_model]
        errors = []
        for model in candidates:
            try:
                r = self.http.post(f"{self.base}/models/{model}:generateContent", json=body, timeout=60)
            except Exception as exc:  # timeout etc. -> try the next model
                errors.append(f"{model}: {type(exc).__name__}")
                continue
            if r.status_code >= 400:
                errors.append(f"{model}: {r.status_code}")
                if r.status_code in (400, 401, 403):
                    break
                continue
            data = r.json()
            text = "".join(
                part.get("text", "")
                for cand in data.get("candidates", [])
                for part in cand.get("content", {}).get("parts", [])
                if not part.get("thought")
            )
            self.chat_model = model
            return text
        raise RuntimeError("No Gemini chat model responded (" + "; ".join(errors) + ")")

    def stream_chat(self, system: str, user: str) -> Iterator[str]:
        text = self.complete(system, user, max_tokens=1536)
        yield text or "I couldn't generate an answer for that. Please rephrase."


SHAPE_MARKER = "Return JSON exactly in this shape:"


class FakeProvider:
    """Hashed bag-of-words embeddings and an extractive 'answer'. No network, fully deterministic.

    When asked for JSON, it fills in the example shape given in the system prompt using text from source [1],
    so every study tool returns a valid, predictable structure offline."""

    dim = 512

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                h = int(hashlib.md5(word.encode()).hexdigest(), 16)
                out[row, h % self.dim] += 1.0
        return out

    @staticmethod
    def _first_source(user: str) -> str:
        m = re.search(r"\[1\][^\n]*\n(.+?)(?:\n\[2\]|\nQuestion:|\nTask:|\Z)", user, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""

    def _fill(self, node, snippet: str, key: str = ""):
        if isinstance(node, dict):
            return {k: self._fill(v, snippet, k) for k, v in node.items()}
        if isinstance(node, list):
            return [self._fill(v, snippet, key) for v in node]
        if isinstance(node, str):
            if key in ("title", "front", "term", "aspect", "question"):
                return snippet[:80] or "Untitled"
            if key in ("year",):
                return "2024"
            if key in ("doi", "url", "venue", "publisher", "volume", "issue", "pages"):
                return ""
            if key in ("given", "family"):
                return "Doe" if key == "family" else "Jane"
            if key == "type":
                return "article"
            return f"(offline demo) {snippet[:160]} [1]" if snippet else "Not stated in the document."
        return node

    def complete(self, system: str, user: str, json_mode: bool = False, max_tokens: int = 1024) -> str:
        if "EXAM_MODE:" in system:
            from .exam import fake_reply  # lazy: exam imports nothing from here
            return "```json\n" + fake_reply(system, user) + "\n```"
        snippet = self._first_source(user)
        if json_mode or SHAPE_MARKER in system:
            m = re.search(re.escape(SHAPE_MARKER) + r"\s*(\{.*?\})\s*(?:\n\n|\Z)", system, re.S)
            shape = json.loads(m.group(1)) if m else {"answer": ""}
            return "```json\n" + json.dumps(self._fill(shape, snippet)) + "\n```"  # fenced, like real models
        if not snippet:
            return "I couldn't find that in your documents."
        return f"(offline demo mode) Based on your documents: {snippet[:300]} [1]"

    def stream_chat(self, system: str, user: str) -> Iterator[str]:
        for word in self.complete(system, user).split(" "):
            yield word + " "


def make_provider(settings) -> Provider:
    if settings.provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError("LLM_PROVIDER=gemini but GEMINI_API_KEY is not set")
        return GeminiProvider(settings.gemini_api_key, settings.gemini_chat_model, settings.gemini_embed_model)
    if settings.provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai but OPENAI_API_KEY is not set")
        return OpenAIProvider(settings.openai_api_key, settings.chat_model, settings.embed_model)
    return FakeProvider()
