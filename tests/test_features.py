"""Tests for v2 features: study/research tools, IndexedDB restore, multi-turn chat, pages, JSON robustness."""
import base64
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.jsonutil import parse_json
from app.main import create_app
from app.providers import FakeProvider
from app.tools import complete_json, format_apa, format_bibtex, format_mla, norm_cite, norm_quiz
from tests.conftest import make_pdf


def read_stream(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line]


def upload(client, pdf, name="paper.pdf"):
    r = client.post("/api/documents", files={"file": (name, pdf, "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def paper_pdf():
    return make_pdf([
        "Deep Learning for Crop Yield Prediction\nAda Obi and Tunde Bello\nAbstract: We predict maize yields from satellite images.",
        "Methodology\nWe trained a convolutional neural network on data from 500 farms in Oyo State.",
        "Results\nThe model reached 92 percent accuracy.\nLimitations: the sample covers a single season.",
    ])


@pytest.fixture
def leave_pdf():
    return make_pdf([
        "Leave policy\nStaff get 25 days of paid annual leave each year.",
        "Remote work policy\nEmployees may work remotely up to three days per week.",
    ])


# ---------- pages ----------

def test_landing_and_app_routes(client):
    r = client.get("/")
    assert r.status_code == 200 and "Every answer cited to the page" in r.text
    assert 'href="/app"' in r.text
    r = client.get("/app")
    assert r.status_code == 200 and "app.js" in r.text
    for path in ("/static/app.js", "/static/app.css", "/static/landing.css", "/favicon.ico"):
        assert client.get(path).status_code == 200, path


def test_security_headers(client):
    r = client.get("/app")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


# ---------- upload payload + restore ----------

def test_upload_returns_index_for_browser_storage(client, paper_pdf):
    doc = upload(client, paper_pdf)
    idx = doc["index"]
    assert doc["pages"] == 3 and idx["dtype"] == "float16"
    assert len(idx["chunks"]) == doc["chunks"]
    raw = base64.b64decode(idx["vectors"])
    assert len(raw) == doc["chunks"] * idx["dim"] * 2
    vecs = np.frombuffer(raw, dtype="<f2").reshape(doc["chunks"], idx["dim"]).astype(np.float32)
    assert np.allclose(np.linalg.norm(vecs, axis=1), 1.0, atol=1e-2)


def restore_body(doc):
    return {"id": doc["id"], "filename": doc["filename"], "pages": doc["pages"], "meta": doc.get("meta", {}),
            **{k: doc["index"][k] for k in ("dim", "dtype", "chunks", "vectors")}}


def test_restore_rehydrates_new_workspace_without_reembedding(client, leave_pdf):
    doc = upload(client, leave_pdf)
    fresh = TestClient(client.app)  # simulates a server restart / new cookie
    assert fresh.get("/api/documents").json() == []
    q = {"question": "How many days can employees work remotely per week?", "doc_ids": [doc["id"]]}
    assert fresh.post("/api/chat", json=q).status_code == 409  # client is told to restore first
    r = fresh.post("/api/documents/restore", json=restore_body(doc))
    assert r.status_code == 200 and r.json()["restored"] is True
    assert fresh.get("/api/documents").json()[0]["pages"] == 2
    # idempotent
    assert fresh.post("/api/documents/restore", json=restore_body(doc)).json()["restored"] is False
    events = read_stream(fresh.post("/api/chat", json=q))
    assert events[0]["sources"][0]["page"] == 2
    assert events[0]["sources"][0]["doc_id"] == doc["id"]
    # restore is free: only the one chat question was charged
    assert fresh.get("/api/usage").json()["questions_used"] == 1


def test_restore_validation(client, leave_pdf):
    doc = upload(client, leave_pdf)
    fresh = TestClient(client.app)
    bad_dim = restore_body(doc) | {"dim": 768}
    assert fresh.post("/api/documents/restore", json=bad_dim).status_code == 422
    bad_len = restore_body(doc) | {"vectors": base64.b64encode(b"\0" * 10).decode()}
    assert fresh.post("/api/documents/restore", json=bad_len).status_code == 422
    bad_b64 = restore_body(doc) | {"vectors": "!!!not-base64!!!"}
    assert fresh.post("/api/documents/restore", json=bad_b64).status_code == 422
    bad_id = restore_body(doc) | {"id": "../etc"}
    assert fresh.post("/api/documents/restore", json=bad_id).status_code == 422
    n, dim = len(doc["index"]["chunks"]), doc["index"]["dim"]
    nan = np.full((n, dim), np.nan, dtype="<f2")
    bad_nan = restore_body(doc) | {"vectors": base64.b64encode(nan.tobytes()).decode()}
    assert fresh.post("/api/documents/restore", json=bad_nan).status_code == 422
    bad_page = restore_body(doc) | {"pages": 1}
    assert fresh.post("/api/documents/restore", json=bad_page).status_code == 422
    assert fresh.get("/api/documents").json() == []


def test_restore_enforces_document_limit(leave_pdf):
    app = create_app(Settings(provider="fake", free_docs_per_workspace=1), provider=FakeProvider())
    a = TestClient(app)
    doc = upload(a, leave_pdf)
    b = TestClient(app)
    upload(b, leave_pdf)
    assert b.post("/api/documents/restore", json=restore_body(doc)).status_code == 402


def test_cookie_survives_server_restart(leave_pdf):
    """After a restart the old cookie id is reused, so the browser can restore into the same workspace id."""
    c1 = TestClient(create_app(Settings(provider="fake"), provider=FakeProvider()))
    c1.get("/api/usage")
    ws_id = c1.cookies.get("dc_ws")
    c2 = TestClient(create_app(Settings(provider="fake"), provider=FakeProvider()), cookies={"dc_ws": ws_id})
    r = c2.get("/api/usage")
    assert "dc_ws" not in r.headers.get("set-cookie", "")


# ---------- multi-turn chat ----------

class SpyProvider(FakeProvider):
    def __init__(self):
        self.prompts = []

    def stream_chat(self, system, user):
        self.prompts.append(user)
        yield from super().stream_chat(system, user)


def test_follow_up_uses_history_for_retrieval_and_prompt(leave_pdf):
    spy = SpyProvider()
    c = TestClient(create_app(Settings(provider="fake", min_score=0.3), provider=spy))
    upload(c, leave_pdf)
    alone = read_stream(c.post("/api/chat", json={"question": "Tell me more about it"}))
    assert alone[0]["sources"] == []  # vague follow-up without context finds nothing
    history = [{"role": "user", "content": "What is the remote work policy?"},
               {"role": "assistant", "content": "Employees may work remotely up to three days per week [1]."}]
    events = read_stream(c.post("/api/chat", json={"question": "Tell me more about it", "history": history}))
    assert events[0]["sources"] and events[0]["sources"][0]["page"] == 2
    prompt = spy.prompts[-1]
    assert "Conversation so far" in prompt and "remote work policy" in prompt
    assert prompt.index("Conversation so far") < prompt.index("Sources:")


def test_history_is_capped(client, leave_pdf):
    upload(client, leave_pdf)
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 5000} for i in range(30)]
    r = client.post("/api/chat", json={"question": "leave days?", "history": history})
    assert r.status_code == 200
    too_many = history + [{"role": "user", "content": "y"}] * 20
    assert client.post("/api/chat", json={"question": "leave?", "history": too_many}).status_code == 422


# ---------- study & research tools ----------

@pytest.mark.parametrize("tool,check", [
    ("summarize", lambda d: d["tldr"] and isinstance(d["key_points"], list)),
    ("explain", lambda d: d["explanation"] and isinstance(d["key_terms"], list)),
    ("flashcards", lambda d: d["cards"] and {"front", "back"} <= set(d["cards"][0])),
    ("quiz", lambda d: d["questions"] and 0 <= d["questions"][0]["answer"] < len(d["questions"][0]["options"])),
    ("brief", lambda d: d["items"][0]["research_question"] and isinstance(d["items"][0]["key_findings"], list)),
    ("cite", lambda d: {"apa", "mla", "bibtex"} <= set(d["items"][0]["formatted"])),
])
def test_tools_return_valid_shapes(client, paper_pdf, tool, check):
    doc = upload(client, paper_pdf)
    r = client.post(f"/api/tools/{tool}", json={"doc_ids": [doc["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tool"] == tool and body["markdown"]
    assert check(body["data"])
    sources = body["sources"] or body["data"]["items"][0]["sources"]
    assert sources and sources[0]["doc_id"] == doc["id"] and sources[0]["page"] >= 1
    assert body["usage"]["questions_used"] == 1


def test_compare_needs_two_docs_and_builds_table(client, paper_pdf, leave_pdf):
    a = upload(client, paper_pdf, "a.pdf")
    assert client.post("/api/tools/compare", json={"doc_ids": [a["id"]]}).status_code == 400
    b = upload(client, leave_pdf, "b.pdf")
    r = client.post("/api/tools/compare", json={"doc_ids": [a["id"], b["id"]], "topic": "purpose, findings"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["documents"] == ["a.pdf", "b.pdf"]
    assert all(len(row["cells"]) == 2 for row in data["rows"])
    assert {s["doc_id"] for s in r.json()["sources"]} == {a["id"], b["id"]}


def test_tools_count_against_quota_and_refund_on_failure(paper_pdf):
    class Broken(FakeProvider):
        def complete(self, system, user, json_mode=False, max_tokens=1024):
            return "sorry, no json here"
    c = TestClient(create_app(Settings(provider="fake", free_questions_per_day=2), provider=Broken()))
    doc = upload(c, paper_pdf)
    r = c.post("/api/tools/summarize", json={"doc_ids": [doc["id"]]})
    assert r.status_code == 502
    assert c.get("/api/usage").json()["questions_used"] == 0  # refunded
    ok = TestClient(create_app(Settings(provider="fake", free_questions_per_day=2), provider=FakeProvider()))
    d = upload(ok, paper_pdf)
    for _ in range(2):
        assert ok.post("/api/tools/flashcards", json={"doc_ids": [d["id"]]}).status_code == 200
    assert ok.post("/api/tools/quiz", json={"doc_ids": [d["id"]]}).status_code == 429


def test_unknown_tool_and_missing_docs(client):
    assert client.post("/api/tools/nope", json={"doc_ids": ["abcd1234"]}).status_code == 404
    r = client.post("/api/tools/summarize", json={"doc_ids": ["abcd1234"]})
    assert r.status_code == 409 and r.json()["missing"] == ["abcd1234"]


def test_prompts_forbid_outside_knowledge(paper_pdf):
    seen = []

    class Spy(FakeProvider):
        def complete(self, system, user, json_mode=False, max_tokens=1024):
            seen.append(system)
            return super().complete(system, user, json_mode, max_tokens)
    c = TestClient(create_app(Settings(provider="fake"), provider=Spy()))
    doc = upload(c, paper_pdf)
    c.post("/api/tools/brief", json={"doc_ids": [doc["id"]]})
    assert "Never add facts from outside knowledge" in seen[-1]
    assert "Not stated in the document." in seen[-1]


# ---------- JSON robustness ----------

def test_parse_json_handles_fences_and_chatter():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: {"a": [1, 2,],} thanks') == {"a": [1, 2]}
    assert parse_json('[{"front": "x"}]') == [{"front": "x"}]
    with pytest.raises(ValueError):
        parse_json("no json at all")


def test_complete_json_retries_once():
    calls = []

    class Flaky:
        def complete(self, system, user, json_mode=False, max_tokens=1024):
            calls.append(user)
            return "oops" if len(calls) == 1 else '{"cards": [{"front": "Q", "back": "A [1]"}]}'
    from app.tools import norm_cards
    out = complete_json(Flaky(), "sys", "user", lambda d: norm_cards(d, {"count": 5}), 100)
    assert out["cards"][0]["front"] == "Q" and len(calls) == 2
    assert "not valid JSON" in calls[1]


def test_quiz_normalizer_validates_and_keeps_answer():
    data = {"questions": [
        {"question": "2+2?", "options": ["4", "3", "5", "6"], "answer": 0, "explanation": "math [1]"},
        {"question": "bad", "options": ["x"], "answer": 0},
        {"question": "out of range", "options": ["a", "b"], "answer": 5},
    ]}
    out = norm_quiz(data, {"count": 8})["questions"]
    assert len(out) == 1 and out[0]["options"][out[0]["answer"]] == "4"


def test_citation_formats():
    c = norm_cite({"type": "article", "title": "Deep Learning for Crops", "authors": [
        {"given": "Ada", "family": "Obi"}, {"given": "Tunde", "family": "Bello"}], "year": "2023",
        "venue": "Journal of AI", "volume": "4", "issue": "2", "pages": "10-20", "doi": "https://doi.org/10.1/xyz"}, {})
    assert format_apa(c) == "Obi, A., & Bello, T. (2023). Deep Learning for Crops. Journal of AI, 4(2), 10-20. https://doi.org/10.1/xyz"
    assert format_mla(c).startswith('Obi, Ada, and Tunde Bello. \u201cDeep Learning for Crops.\u201d Journal of AI, vol. 4, no. 2, 2023')
    bib = format_bibtex(c)
    assert bib.startswith("@article{obi2023deep,") and "author = {Obi, Ada and Bello, Tunde}" in bib and "pages = {10--20}" in bib
