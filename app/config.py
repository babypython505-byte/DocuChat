"""Runtime settings, read from environment variables."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_chat_model: str = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.5-flash-lite")
    gemini_embed_model: str = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001")
    chat_model: str = os.getenv("CHAT_MODEL", "gpt-4o-mini")
    embed_model: str = os.getenv("EMBED_MODEL", "text-embedding-3-small")
    chunk_size: int = int(os.getenv("CHUNK_SIZE", "900"))        # characters
    chunk_overlap: int = int(os.getenv("CHUNK_OVERLAP", "150"))  # characters
    top_k: int = int(os.getenv("TOP_K", "5"))
    min_score: float = float(os.getenv("MIN_SCORE", "0.2"))      # cosine floor for "relevant"
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "20"))
    free_questions_per_day: int = int(os.getenv("FREE_QUESTIONS_PER_DAY", "20"))
    free_docs_per_workspace: int = int(os.getenv("FREE_DOCS_PER_WORKSPACE", "3"))
    free_exam_sets_per_day: int = int(os.getenv("FREE_EXAM_SETS_PER_DAY", "5"))
    free_exam_marks_per_day: int = int(os.getenv("FREE_EXAM_MARKS_PER_DAY", "40"))  # theory-marking requests
    # ~1,200 passages is roughly a 400-page book; caps RAM per document on a 512 MB instance
    max_chunks_per_doc: int = int(os.getenv("MAX_CHUNKS_PER_DOC", "1200"))
    # Supabase Auth. The project URL and the publishable (anon) key are public by design: they ship to the browser,
    # and Row Level Security protects the data. Never put the service-role key here; it isn't needed.
    supabase_url: str = os.getenv("SUPABASE_URL", "https://lxkytyhrsotaaeyfwnac.supabase.co").rstrip("/")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "sb_publishable_E9H58ssFh7CYu4AYFH6PjQ_SniBKhzW")
    auth_cache_seconds: int = int(os.getenv("AUTH_CACHE_SECONDS", "60"))
    # "gemini", "openai", or "fake" (offline demos and tests)
    provider: str = os.getenv(
        "LLM_PROVIDER",
        "gemini" if os.getenv("GEMINI_API_KEY") else "openai" if os.getenv("OPENAI_API_KEY") else "fake",
    )


settings = Settings()
