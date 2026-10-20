"""DocuChat: upload PDFs, ask questions, get answers with page-level citations."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .ingest import chunk_pdf
from .providers import make_provider
from .rag import RagEngine, citations
from .workspaces import Quota, Workspace, Workspaces

STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "dc_ws"


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    doc_ids: list[str] | None = None


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def create_app(settings: Settings = default_settings, provider=None) -> FastAPI:
    app = FastAPI(title="DocuChat RAG", version="1.1.0")
    provider = provider or make_provider(settings)
    workspaces = Workspaces()
    questions = Quota()
    limit = settings.free_questions_per_day

    def workspace(request: Request, response: Response) -> Workspace:
        ws = workspaces.get_or_create(request.cookies.get(COOKIE))
        if request.cookies.get(COOKIE) != ws.id:
            response.set_cookie(COOKIE, ws.id, max_age=86400, httponly=True, samesite="lax",
                                secure=request.url.scheme == "https")
        return ws

    def usage(ws: Workspace, ip: str) -> dict:
        used = max(questions.used(f"ws:{ws.id}"), questions.used(f"ip:{ip}"))
        return {"questions_used": used, "questions_limit": limit,
                "docs_used": len(ws.store.documents()), "docs_limit": settings.free_docs_per_workspace}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "provider": type(provider).__name__}

    @app.get("/api/usage")
    def get_usage(request: Request, ws: Workspace = Depends(workspace)):
        return usage(ws, client_ip(request))

    @app.post("/api/documents", status_code=201)
    async def upload(file: UploadFile = File(...), ws: Workspace = Depends(workspace)):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF files are supported")
        if len(ws.store.documents()) >= settings.free_docs_per_workspace:
            raise HTTPException(402, f"Free plan allows {settings.free_docs_per_workspace} documents. "
                                     "Remove one or upgrade to Pro.")
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
        try:
            vectors = provider.embed([c.text for c in chunks])
        except Exception as exc:
            raise HTTPException(503, "The AI service is busy. Please try again in a minute.") from exc
        ws.store.add(chunks, vectors)
        return {"id": doc_id, "filename": file.filename, "chunks": len(chunks),
                "pages": max(c.page for c in chunks)}

    @app.get("/api/documents")
    def list_documents(ws: Workspace = Depends(workspace)):
        return ws.store.documents()

    @app.delete("/api/documents/{doc_id}")
    def delete_document(doc_id: str, ws: Workspace = Depends(workspace)):
        if ws.store.delete(doc_id) == 0:
            raise HTTPException(404, "Document not found")
        return {"deleted": doc_id}

    @app.post("/api/chat")
    def chat(req: ChatRequest, request: Request, ws: Workspace = Depends(workspace)):
        """Streams NDJSON: one {"type":"sources"} line, then {"type":"token"} lines, then {"type":"done"}."""
        ip = client_ip(request)
        if not questions.take([f"ws:{ws.id}", f"ip:{ip}"], limit):
            raise HTTPException(429, f"You've used today's {limit} free questions. Come back tomorrow or upgrade to Pro.")
        engine = RagEngine(provider, ws.store, settings.top_k, settings.min_score)
        hits = engine.retrieve(req.question, req.doc_ids)

        def events():
            yield json.dumps({"type": "sources", "sources": citations(hits)}) + "\n"
            try:
                for token in engine.stream_answer(req.question, hits):
                    yield json.dumps({"type": "token", "text": token}) + "\n"
            except Exception:
                yield json.dumps({"type": "error", "message": "The AI service hiccuped. Please ask again."}) + "\n"
            yield json.dumps({"type": "done", "usage": usage(ws, ip)}) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
