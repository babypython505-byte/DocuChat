"""DocuChat: upload PDFs, ask questions, get answers with page-level citations, plus study & research tools."""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .ingest import Chunk, chunk_pages, parse_pdf
from .providers import make_provider
from .rag import RagEngine, citations, trim_history
from .tools import TOOLS, ToolInputError, ToolOutputError, cost, run_tool
from .workspaces import Quota, Workspace, Workspaces

log = logging.getLogger("docuchat")

STATIC = Path(__file__).resolve().parent.parent / "static"
COOKIE = "dc_ws"
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
BUSY = "The AI service is busy. Please try again in a minute."


def ai_error(exc: Exception) -> str:
    """Short, key-free description of an upstream AI error."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            msg = resp.json().get("error", {}).get("message", "")
        except Exception:
            msg = resp.text[:200]
        return f"upstream {resp.status_code}: {msg[:200]}"
    return str(exc)[:200] or type(exc).__name__


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    doc_ids: list[str] | None = Field(default=None, max_length=20)
    history: list[Turn] = Field(default_factory=list, max_length=40)


class ToolRequest(BaseModel):
    doc_ids: list[str] = Field(min_length=1, max_length=20)
    topic: str | None = Field(default=None, max_length=500)
    count: int | None = Field(default=None, ge=1, le=50)
    difficulty: str | None = Field(default=None, max_length=10)


class RestoreChunk(BaseModel):
    page: int = Field(ge=1, le=10000)
    text: str = Field(min_length=1, max_length=4000)


class RestoreRequest(BaseModel):
    id: str = Field(min_length=4, max_length=40)
    filename: str = Field(min_length=1, max_length=255)
    pages: int = Field(ge=1, le=10000)
    meta: dict = Field(default_factory=dict)
    dim: int = Field(ge=8, le=4096)
    dtype: Literal["float16", "float32"] = "float16"
    chunks: list[RestoreChunk] = Field(min_length=1)
    vectors: str = Field(min_length=4)  # base64 of row-major (n, dim) little-endian floats


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def encode_vectors(vectors: np.ndarray) -> str:
    """Normalised vectors as base64 float16 (2 bytes/dim): ~2 KB per 768-dim chunk, ample precision for cosine."""
    v = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return base64.b64encode((v / norms).astype("<f2").tobytes()).decode("ascii")


def clean_meta(meta: dict) -> dict:
    out = {}
    for k in ("title", "author", "subject", "created"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = v.strip()[:300]
    return out


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; font-src 'self' data: https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; worker-src 'self' blob: https://cdn.jsdelivr.net; "
        "connect-src 'self' https://cdn.jsdelivr.net; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


def create_app(settings: Settings = default_settings, provider=None) -> FastAPI:
    app = FastAPI(title="DocuChat", version="2.0.0")
    provider = provider or make_provider(settings)
    workspaces = Workspaces()
    questions = Quota()
    limit = settings.free_questions_per_day
    doc_limit = settings.free_docs_per_workspace
    provider_dim = getattr(provider, "dim", None)
    app.state.workspaces = workspaces  # exposed for tests / QA harness (simulate a restart by clearing it)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def workspace(request: Request, response: Response) -> Workspace:
        ws = workspaces.get_or_create(request.cookies.get(COOKIE))
        if request.cookies.get(COOKIE) != ws.id:
            response.set_cookie(COOKIE, ws.id, max_age=30 * 86400, httponly=True, samesite="lax",
                                secure=request.url.scheme == "https")
        return ws

    def usage(ws: Workspace, ip: str) -> dict:
        used = max(questions.used(f"ws:{ws.id}"), questions.used(f"ip:{ip}"))
        return {"questions_used": used, "questions_limit": limit,
                "docs_used": len(ws.store.documents()), "docs_limit": doc_limit}

    def quota_keys(ws: Workspace, request: Request) -> list[str]:
        return [f"ws:{ws.id}", f"ip:{client_ip(request)}"]

    def charge(ws: Workspace, request: Request, n: int = 1) -> list[str]:
        keys = quota_keys(ws, request)
        if not questions.take(keys, limit, n):
            raise HTTPException(429, f"You've used today's {limit} free questions. Come back tomorrow or join the Pro waitlist.")
        return keys

    def require_docs(ws: Workspace, doc_ids: list[str] | None) -> None:
        """409 tells the browser to re-hydrate these documents from IndexedDB and retry (server restarted)."""
        missing = [d for d in (doc_ids or []) if not ws.store.has(d)]
        if missing:
            raise HTTPException(409, {"code": "missing_documents", "missing": missing,
                                      "message": "Your documents need to be re-synced. Retrying…"})

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail
        body = {"detail": detail["message"], **detail} if isinstance(detail, dict) else {"detail": detail}
        return JSONResponse(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.get("/api/health")
    def health():
        return {"status": "ok", "provider": type(provider).__name__, "version": app.version}

    @app.get("/api/usage")
    def get_usage(request: Request, ws: Workspace = Depends(workspace)):
        return usage(ws, client_ip(request))

    @app.get("/api/config")
    def get_config():
        return {"questions_limit": limit, "docs_limit": doc_limit, "max_upload_mb": settings.max_upload_mb,
                "tools": {k: t.title for k, t in TOOLS.items()}, "provider": type(provider).__name__}

    @app.post("/api/documents", status_code=201)
    async def upload(request: Request, file: UploadFile = File(...), ws: Workspace = Depends(workspace)):
        if not (file.filename or "").lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF files are supported")
        if len(ws.store.documents()) >= doc_limit:
            raise HTTPException(402, f"The free plan holds {doc_limit} documents at a time. Remove one to add another.")
        data = await file.read()
        if len(data) > settings.max_upload_mb * 1024 * 1024:
            raise HTTPException(413, f"File is larger than {settings.max_upload_mb} MB")
        doc_id = uuid.uuid4().hex[:12]
        filename = Path(file.filename).name[:255]
        try:
            parsed = parse_pdf(data)
        except Exception as exc:  # pypdf raises several error types for broken or encrypted files
            raise HTTPException(400, f"Could not read PDF: {str(exc)[:200]}") from exc
        chunks = chunk_pages(doc_id, filename, parsed.pages, settings.chunk_size, settings.chunk_overlap)
        if not chunks:
            raise HTTPException(422, "No extractable text found (scanned PDFs need OCR first)")
        if len(chunks) > settings.max_chunks_per_doc:
            raise HTTPException(413, f"This PDF is too long for the free plan (over {settings.max_chunks_per_doc} passages). "
                                     "Try splitting it into parts.")
        try:
            vectors = provider.embed([c.text for c in chunks])
        except Exception as exc:
            log.exception("embedding failed")
            raise HTTPException(503, f"{BUSY} ({ai_error(exc)})") from exc
        pages = len(parsed.pages)
        ws.store.add(chunks, vectors, pages=pages, meta=parsed.meta)
        return {"id": doc_id, "filename": filename, "chunks": len(chunks), "pages": pages, "meta": parsed.meta,
                "index": {"dim": int(vectors.shape[1]), "dtype": "float16",
                          "chunks": [{"page": c.page, "text": c.text} for c in chunks],
                          "vectors": encode_vectors(vectors)},
                "usage": usage(ws, client_ip(request))}

    @app.post("/api/documents/restore")
    def restore(req: RestoreRequest, request: Request, ws: Workspace = Depends(workspace)):
        """Re-hydrate a document from the browser's IndexedDB copy (no re-embedding, no quota)."""
        if not DOC_ID_RE.match(req.id):
            raise HTTPException(422, "Invalid document id")
        if ws.store.has(req.id):
            return {"id": req.id, "restored": False, "usage": usage(ws, client_ip(request))}
        if len(ws.store.documents()) >= doc_limit:
            raise HTTPException(402, f"The free plan holds {doc_limit} documents at a time.")
        n = len(req.chunks)
        if n > settings.max_chunks_per_doc:
            raise HTTPException(413, "Too many chunks for one document")
        expected_dim = ws.store.dim or provider_dim
        if expected_dim and req.dim != expected_dim:
            raise HTTPException(422, f"Vector dimension {req.dim} does not match the server's model ({expected_dim}). "
                                     "Please re-upload this PDF.")
        if any(c.page > req.pages for c in req.chunks):
            raise HTTPException(422, "Chunk page is outside the document")
        itemsize = 2 if req.dtype == "float16" else 4
        if len(req.vectors) > (n * req.dim * itemsize * 4) // 3 + 8:
            raise HTTPException(413, "Vector payload too large")
        try:
            raw = base64.b64decode(req.vectors, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(422, "Vectors are not valid base64") from exc
        if len(raw) != n * req.dim * itemsize:
            raise HTTPException(422, f"Expected {n}x{req.dim} {req.dtype} vectors")
        vectors = np.frombuffer(raw, dtype="<f2" if itemsize == 2 else "<f4").astype(np.float32).reshape(n, req.dim)
        if not np.isfinite(vectors).all():
            raise HTTPException(422, "Vectors contain invalid numbers")
        filename = Path(req.filename).name[:255]
        chunks = [Chunk(doc_id=req.id, filename=filename, page=c.page, text=c.text) for c in req.chunks]
        try:
            ws.store.add(chunks, vectors, pages=req.pages, meta=clean_meta(req.meta))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"id": req.id, "restored": True, "usage": usage(ws, client_ip(request))}

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
        require_docs(ws, req.doc_ids)
        keys = charge(ws, request)
        ip = client_ip(request)
        history = trim_history([t.model_dump() for t in req.history])
        engine = RagEngine(provider, ws.store, settings.top_k, settings.min_score)
        try:
            hits = engine.retrieve(req.question, req.doc_ids or None, history)
        except Exception as exc:
            questions.give_back(keys)
            log.exception("query embedding failed")
            raise HTTPException(503, f"{BUSY} ({ai_error(exc)})") from exc

        def events():
            yield json.dumps({"type": "sources", "sources": citations(hits)}) + "\n"
            try:
                for token in engine.stream_answer(req.question, hits, history):
                    yield json.dumps({"type": "token", "text": token}) + "\n"
            except Exception as exc:
                questions.give_back(keys)
                log.exception("chat failed")
                yield json.dumps({"type": "error", "message": f"The AI service hiccuped, so this one is on us. Please ask again. ({ai_error(exc)})"}) + "\n"
            yield json.dumps({"type": "done", "usage": usage(ws, ip)}) + "\n"

        # "no-store" on a progressively-read stream makes Chromium report the request as aborted (net::ERR_ABORTED)
        # even though every byte arrived; POST responses are never cached, so "no-cache" is equivalent here.
        # no-transform + X-Accel-Buffering stop proxies from buffering or compressing the token stream.
        return StreamingResponse(events(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

    @app.post("/api/tools/{tool}")
    def tool(tool: str, req: ToolRequest, request: Request, ws: Workspace = Depends(workspace)):
        if tool not in TOOLS:
            raise HTTPException(404, f"Unknown tool '{tool}'")
        doc_ids = list(dict.fromkeys(req.doc_ids))
        require_docs(ws, doc_ids)
        if len(doc_ids) < TOOLS[tool].min_docs:
            raise HTTPException(400, f"{TOOLS[tool].title} needs at least {TOOLS[tool].min_docs} documents selected.")
        n = cost(tool, doc_ids)
        keys = charge(ws, request, n)
        try:
            result = run_tool(provider, ws.store, tool, doc_ids, req.model_dump())
        except ToolInputError as exc:
            questions.give_back(keys, n)
            raise HTTPException(400, str(exc)) from exc
        except ToolOutputError as exc:
            questions.give_back(keys, n)
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            questions.give_back(keys, n)
            log.exception("tool %s failed", tool)
            raise HTTPException(503, f"{BUSY} ({ai_error(exc)})") from exc
        result["usage"] = usage(ws, client_ip(request))
        result["cost"] = n
        return result

    @app.get("/")
    def landing():
        return FileResponse(STATIC / "landing.html")

    @app.get("/app")
    def app_page():
        return FileResponse(STATIC / "app.html")

    @app.get("/favicon.ico")
    def favicon():
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
