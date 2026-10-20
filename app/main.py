"""DocuChat: upload PDFs, ask questions, get answers with page-level citations."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .ingest import chunk_pdf
from .providers import make_provider
from .rag import RagEngine, citations
from .store import VectorStore

STATIC = Path(__file__).resolve().parent.parent / "static"


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    doc_ids: list[str] | None = None


def create_app(settings: Settings = default_settings, provider=None) -> FastAPI:
    app = FastAPI(title="DocuChat RAG", version="1.0.0")
    store = VectorStore()
    provider = provider or make_provider(settings)
    engine = RagEngine(provider, store, settings.top_k, settings.min_score)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "provider": type(provider).__name__}

    @app.post("/api/documents", status_code=201)
    async def upload(file: UploadFile = File(...)):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF files are supported")
        data = await file.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
        doc_id = uuid.uuid4().hex[:12]
        try:
            chunks = chunk_pdf(doc_id, file.filename, data, settings.chunk_size, settings.chunk_overlap)
        except Exception as exc:  # pypdf raises several error types for broken files
            raise HTTPException(400, f"Could not read PDF: {exc}") from exc
        if not chunks:
            raise HTTPException(422, "No extractable text found (scanned PDFs need OCR first)")
        store.add(chunks, provider.embed([c.text for c in chunks]))
        return {"id": doc_id, "filename": file.filename, "chunks": len(chunks),
                "pages": max(c.page for c in chunks)}

    @app.get("/api/documents")
    def list_documents():
        return store.documents()

    @app.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: str):
        if store.delete(doc_id) == 0:
            raise HTTPException(404, "Document not found")
        return {"deleted": doc_id}

    @app.post("/api/chat")
    def chat(req: ChatRequest):
        """Streams NDJSON: one {"type":"sources"} line, then {"type":"token"} lines, then {"type":"done"}."""
        hits = engine.retrieve(req.question, req.doc_ids)

        def events():
            yield json.dumps({"type": "sources", "sources": citations(hits)}) + "\n"
            try:
                for token in engine.stream_answer(req.question, hits):
                    yield json.dumps({"type": "token", "text": token}) + "\n"
            except Exception as exc:
                yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
            yield json.dumps({"type": "done"}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
