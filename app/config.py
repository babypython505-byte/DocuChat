"""Runtime settings, read from environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    chat_model: str = os.getenv("CHAT_MODEL", "gpt-4o-mini")
    embed_model: str = os.getenv("EMBED_MODEL", "text-embedding-3-small")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "900"))        # characters
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "150"))  # characters
    top_k: int = int(os.getenv("TOP_K", "5"))
    min_score: float = float(os.getenv("MIN_SCORE", "0.2"))      # cosine floor for "relevant"
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "20"))
    # "openai" for real models, "fake" for offline demos and tests
    provider: str = os.getenv("LLM_PROVIDER", "openai" if os.getenv("OPENAI_API_KEY") else "fake")


settings = Settings()
