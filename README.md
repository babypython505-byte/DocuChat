# DocuChat: chat with your PDFs, with page citations

A production-minded **RAG (retrieval-augmented generation)** app. Upload PDFs (contracts, handbooks, reports), ask questions in plain English, and get streamed answers where **every claim cites the file and page it came from**. If the answer isn't in your documents, it says so instead of guessing.

Built by **Musendiq Oladimeji**, AI developer and founder of Altix Africa · [altixmanger.com](https://altixmanger.com)

---

## Features
- **PDF ingestion**: page-aware chunking that breaks on paragraphs and sentences, with overlap
- **Semantic search**: OpenAI embeddings (`text-embedding-3-small`) and cosine similarity
- **Grounded answers**: numbered sources in the prompt, strict "answer only from sources" rules, and a relevance floor so the bot refuses off-topic questions
- **Streaming**: an NDJSON stream sends sources first, then tokens, for a fast-feeling UI
- **Clean web UI**: drag-and-drop upload, document list, citation chips with snippet previews
- **Offline demo mode**: `LLM_PROVIDER=fake` runs everything with no API key (deterministic, used in tests)
- **Tested and shippable**: pytest suite, Dockerfile, GitHub Actions CI

## Architecture
```
PDF ──► pypdf (per page) ──► chunker (size 900, overlap 150) ──► embeddings ──► vector store
                                                                                   │
question ──► embed ──► top-k cosine search (+ min score) ──► prompt with [n] sources ──► LLM (streamed)
```
| Module | Role |
|---|---|
| `app/ingest.py` | PDF text extraction and boundary-aware chunking |
| `app/store.py` | Thread-safe in-memory vector store (swap for pgvector or Qdrant) |
| `app/providers.py` | OpenAI provider plus an offline fake provider behind one interface |
| `app/rag.py` | Retrieval, prompt construction, citations |
| `app/main.py` | FastAPI endpoints and static UI |

## Quick start
```bash
git clone https://github.com/babypython505-byte/DocuChat.git && cd DocuChat
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add your OPENAI_API_KEY
export $(cat .env | xargs)
uvicorn app.main:app --reload   # open http://localhost:8000
```
No key? Run `LLM_PROVIDER=fake uvicorn app.main:app` to try the full flow offline.

### Docker
```bash
docker build -t docuchat . && docker run -p 8000:8000 -e OPENAI_API_KEY=sk-... docuchat
```

## API
| Method | Path | Description |
|---|---|---|
| `POST` | `/api/documents` | Upload a PDF (multipart `file`) and index it |
| `GET` | `/api/documents` | List indexed documents |
| `DELETE` | `/api/documents/{id}` | Remove a document |
| `POST` | `/api/chat` | `{"question": "...", "doc_ids": [optional]}` streams NDJSON events: `sources`, `token`, `done` |

## Tests
```bash
pip install -r requirements-dev.txt && pytest -q
```

## Production roadmap
- Persistent vector DB (pgvector or Qdrant) and per-user workspaces
- Hybrid search (BM25 plus vectors) with a reranker
- OCR for scanned PDFs, and DOCX, HTML and Notion sources
- Auth, rate limiting, and usage metering
- Evaluation harness (answer faithfulness, retrieval recall)

## Hire me
I build custom AI chatbots, agents, and automations for startups and businesses: on your docs, your CRM, or your WhatsApp.
**Email:** babypython505@gmail.com · **Contra:** [contra.com/baby_python_0d8w5tci](https://contra.com/baby_python_0d8w5tci)

MIT License
