"""Exam Prep: page-range extraction, generation parsing/validation, marking, limits and error handling."""
import json

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.exam import (FRIENDLY_FAIL, _merge_overlap, mark_mcq, normalize_marks, normalize_questions,
                      normalize_theory, page_texts, parse_pages, resolve_request)
from app.main import create_app
from app.providers import FakeProvider
from tests.conftest import make_pdf

BIO = [
    "Photosynthesis\nPlants make glucose from carbon dioxide and water using light energy.\nChlorophyll absorbs light.",
    "Respiration\nCells release energy from glucose in the mitochondria.\nOxygen is used and carbon dioxide is released.",
    "Osmosis\nWater moves across a partially permeable membrane from a dilute to a concentrated solution.",
    "Diffusion\nParticles spread from a region of high concentration to low concentration.",
]


class Scripted(FakeProvider):
    """Fake embeddings, but chat replies come from a script (one reply per call; the last one repeats)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user, json_mode=False, max_tokens=1024):
        self.calls.append((system, user))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def make_client(provider=None, **kw):
    return TestClient(create_app(Settings(provider="fake", min_score=0.05, **kw), provider=provider or FakeProvider()))


def upload(client, pages=BIO):
    r = client.post("/api/documents", files={"file": ("bio.pdf", make_pdf(pages), "application/pdf")})
    assert r.status_code == 201, r.text
    return r.json()


MCQ_JSON = {"questions": [
    {"type": "mcq", "question": "Where is energy released from glucose?", "options": ["Nucleus", "Mitochondria", "Ribosome", "Cell wall"],
     "answer": 1, "explanation": "Respiration happens in the mitochondria.", "pages": [2], "topic": "Respiration", "difficulty": "easy"},
    {"type": "mcq", "question": "What does chlorophyll do?", "options": ["A. Absorbs light", "B. Stores water", "C. Makes oxygen", "D. Kills germs"],
     "answer": "A", "explanation": "Chlorophyll absorbs light.", "pages": "p. 1", "topic": "Photosynthesis"},
]}
THEORY_JSON = {"questions": [
    {"type": "theory", "question": "Photosynthesis", "parts": [{"label": "(a)", "text": "Define photosynthesis.", "marks": 2},
                                                               {"label": "(b)", "text": "Name the pigment that absorbs light.", "marks": 1}],
     "marks": 3, "model_answer": "(a) Plants make glucose from CO2 and water using light. (b) Chlorophyll.",
     "marking_points": [{"point": "glucose made from carbon dioxide and water", "marks": 1}, {"point": "using light energy", "marks": 1},
                        {"point": "chlorophyll", "marks": 1}], "pages": [1], "topic": "Photosynthesis", "difficulty": "easy"},
]}


# ---------- parsing and validation ----------

def test_merge_overlap_rebuilds_page_without_duplicates():
    a = "The quick brown fox jumps over the lazy dog and keeps running far away"
    b = "lazy dog and keeps running far away into the forest"
    assert _merge_overlap(a, b) == "The quick brown fox jumps over the lazy dog and keeps running far away into the forest"
    assert _merge_overlap("", "x") == "x"


def test_page_texts_come_from_chunks():
    c = make_client()
    doc = upload(c)
    store = c.app.state.workspaces._items[next(iter(c.app.state.workspaces._items))].store
    pages = page_texts(store, doc["id"])
    assert sorted(pages) == [1, 2, 3, 4]
    assert "mitochondria" in pages[2] and "mitochondria" not in pages[1]


def test_parse_pages_keeps_range():
    assert parse_pages([2, "p. 4-5", "9"], 1, 6) == [2, 4, 5]
    assert parse_pages("pages 3 and 3", 1, 6) == [3]
    assert parse_pages(None, 1, 6) == []


def test_resolve_request_clamps_and_splits_mixed():
    r = resolve_request({"qtype": "mixed", "count": 7, "page_from": 0, "page_to": 99, "style": "bogus"}, 10)
    assert (r["n_mcq"], r["n_theory"], r["page_from"], r["page_to"], r["style"]) == (5, 2, 1, 10, "general")
    assert resolve_request({"qtype": "mcq", "count": 500}, 3)["count"] == 50
    with pytest.raises(ValueError):
        resolve_request({"page_from": 5, "page_to": 2}, 10)


def test_mcq_parsing_accepts_letters_and_strips_prefixes():
    qs = normalize_questions(MCQ_JSON, "mcq", 1, 4, {1: "chlorophyll", 2: "mitochondria"})
    assert len(qs) == 2
    for q, src in zip(qs, MCQ_JSON["questions"]):
        correct = src["options"][1] if src["answer"] == 1 else "Absorbs light"
        assert q["options"][q["answer"]] == correct
        assert all(not o.startswith(("A.", "B.")) for o in q["options"])
    assert qs[1]["pages"] == [1]


def test_mcq_parsing_drops_invalid_questions():
    bad = {"questions": [
        {"question": "No answer", "options": ["a", "b", "c", "d"], "answer": 7},
        {"question": "Duplicate options", "options": ["a", "a", "b", "c"], "answer": 0},
        {"question": "Too few", "options": ["a", "b"], "answer": 0},
        {"question": "Good one", "options": ["w", "x", "y", "z"], "answer": "y", "pages": [99]},
    ]}
    qs = normalize_questions(bad, "mcq", 1, 4, {3: "good one y"})
    assert len(qs) == 1 and qs[0]["options"][qs[0]["answer"]] == "y"
    assert qs[0]["pages"] == [3]  # out-of-range page replaced by the best-matching page in range
    with pytest.raises(ValueError):
        normalize_questions({"questions": [bad["questions"][0]]}, "mcq", 1, 4, {})


def test_theory_parsing_makes_marks_add_up():
    q = normalize_questions(THEORY_JSON, "theory", 1, 4, {})[0]
    assert q["marks"] == 3 == sum(p["marks"] for p in q["marking_points"]) == sum(p["marks"] for p in q["parts"])
    assert [p["label"] for p in q["parts"]] == ["a", "b"]
    uneven = dict(THEORY_JSON["questions"][0], parts=[{"label": "a", "text": "x", "marks": 5}, {"label": "b", "text": "y", "marks": 5}])
    q2 = normalize_theory(uneven, 1, 4, {})
    assert sum(p["marks"] for p in q2["parts"]) == q2["marks"] == 3
    assert normalize_theory({"question": "No scheme", "marking_points": []}, 1, 4, {}) is None


# ---------- generation endpoint ----------

def test_generate_mixed_set_with_fake_provider():
    c = make_client()
    doc = upload(c)
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "mixed", "count": 7, "style": "waec",
                                           "difficulty": "mixed", "page_from": 2, "page_to": 3})
    assert r.status_code == 200, r.text
    exam = r.json()
    types = [q["type"] for q in exam["questions"]]
    assert types.count("mcq") == 5 and types.count("theory") == 2
    assert all(set(q["pages"]) <= {2, 3} and q["pages"] for q in exam["questions"])
    assert [q["id"] for q in exam["questions"]] == [f"q{i}" for i in range(1, 8)]
    assert exam["usage"]["exams_used"] == 1 and exam["usage"]["questions_used"] == 0


def test_generation_sends_only_the_chosen_pages():
    p = Scripted([json.dumps(MCQ_JSON)])
    c = make_client(p)
    doc = upload(c)
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "mcq", "count": 5, "page_from": 2, "page_to": 2})
    assert r.status_code == 200, r.text
    user = p.calls[0][1]
    assert "=== Page 2 ===" in user and "mitochondria" in user
    assert "=== Page 1 ===" not in user and "Osmosis" not in user
    assert "WAEC" not in p.calls[0][0]  # default style is general


def test_generation_retries_invalid_json_once_then_succeeds():
    p = Scripted(["Sure! Here are your questions: {oops", "```json\n" + json.dumps(MCQ_JSON) + "\n```"])
    c = make_client(p)
    doc = upload(c)
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "mcq", "count": 5})
    assert r.status_code == 200, r.text
    assert "previous reply was not valid JSON" in p.calls[1][1]


def test_generation_gives_friendly_error_and_refund_after_two_bad_replies():
    p = Scripted(["not json at all"])
    c = make_client(p)
    doc = upload(c)
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "mcq", "count": 5})
    assert r.status_code == 502
    assert r.json()["detail"] == FRIENDLY_FAIL
    assert len(p.calls) == 2
    assert c.get("/api/usage").json()["exams_used"] == 0


def test_generation_upstream_error_is_friendly_and_refunded():
    class Down(FakeProvider):
        def complete(self, *a, **k):
            raise RuntimeError("No Gemini chat model responded (429)")
    c = make_client(Down())
    doc = upload(c)
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "count": 5})
    assert r.status_code == 503 and "try again" in r.json()["detail"]
    assert c.get("/api/usage").json()["exams_used"] == 0


def test_daily_exam_set_limit():
    c = make_client(free_exam_sets_per_day=2, guest_exam_sets_per_day=2)
    doc = upload(c)
    body = {"doc_id": doc["id"], "count": 5}
    assert c.post("/api/exam/generate", json=body).status_code == 200
    assert c.post("/api/exam/generate", json=body).status_code == 200
    r = c.post("/api/exam/generate", json=body)
    assert r.status_code == 429 and "exam" in r.json()["detail"]


def test_generate_validation_and_missing_docs():
    c = make_client()
    doc = upload(c)
    assert c.post("/api/exam/generate", json={"doc_id": doc["id"], "count": 4}).status_code == 422
    assert c.post("/api/exam/generate", json={"doc_id": doc["id"], "count": 51}).status_code == 422
    assert c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "essay"}).status_code == 422
    r = c.post("/api/exam/generate", json={"doc_id": doc["id"], "page_from": 4, "page_to": 2})
    assert r.status_code == 400
    r = c.post("/api/exam/generate", json={"doc_id": "missingdoc1", "count": 5})
    assert r.status_code == 409 and r.json()["code"] == "missing_documents"


# ---------- marking ----------

def theory_q():
    return normalize_questions(THEORY_JSON, "theory", 1, 4, {})[0]


def test_mark_mcq_is_deterministic():
    q = normalize_questions(MCQ_JSON, "mcq", 1, 4, {})[0]
    assert mark_mcq(q, q["answer"])["correct"] is True
    wrong = mark_mcq(q, (q["answer"] + 1) % 4)
    assert wrong["correct"] is False and wrong["score"] == 0 and wrong["answer"] == q["answer"]
    assert mark_mcq(q, None)["score"] == 0


def test_normalize_marks_clamps_scores_per_point():
    q = theory_q()
    items = [{"id": "q1", "question": q, "answer": "x"}]
    data = {"results": [{"id": "q1", "awarded": [{"point": 0, "marks": 5}, {"point": 1, "marks": 0.5}, {"point": 9, "marks": 3}],
                         "good": "Defined it", "missing": ["chlorophyll"], "feedback": "Name the pigment."}]}
    r = normalize_marks(data, items)["q1"]
    assert r["score"] == 1.5 and r["max"] == 3
    assert r["good"] == ["Defined it"] and r["model_answer"].startswith("(a)")
    assert [p["awarded"] for p in r["points"]] == [1, 0.5, 0]


def test_mark_endpoint_mcq_needs_no_ai_and_no_quota():
    class NoAI(FakeProvider):
        def complete(self, *a, **k):
            raise AssertionError("MCQ marking must not call the AI")
    c = make_client(NoAI(), free_exam_marks_per_day=0, guest_exam_marks_per_day=0)
    q = normalize_questions(MCQ_JSON, "mcq", 1, 4, {})[0]
    r = c.post("/api/exam/mark", json={"items": [{"id": "q1", "question": q, "choice": q["answer"]}]})
    assert r.status_code == 200
    assert r.json()["results"]["q1"]["correct"] is True


def test_mark_endpoint_theory_with_scripted_marker():
    reply = {"results": [{"id": "q2", "awarded": [{"point": 0, "marks": 1}, {"point": 1, "marks": 1}, {"point": 2, "marks": 0}],
                          "good": ["Correct definition"], "missing": ["Name chlorophyll"], "feedback": "Good start."}]}
    p = Scripted([json.dumps(reply)])
    c = make_client(p)
    r = c.post("/api/exam/mark", json={"style": "waec", "items": [
        {"id": "q2", "question": theory_q(), "answer": "Plants use light to make glucose from carbon dioxide and water."}]})
    assert r.status_code == 200, r.text
    res = r.json()["results"]["q2"]
    assert (res["score"], res["max"]) == (2, 3)
    assert res["missing"] == ["Name chlorophyll"] and res["model_answer"]
    assert "WAEC" in p.calls[0][0] and "Plants use light" in p.calls[0][1]
    assert r.json()["usage"]["marks_used"] == 1


def test_mark_blank_theory_answer_scores_zero_without_ai():
    class NoAI(FakeProvider):
        def complete(self, *a, **k):
            raise AssertionError("blank answers must not call the AI")
    c = make_client(NoAI())
    r = c.post("/api/exam/mark", json={"items": [{"id": "q1", "question": theory_q(), "answer": "  "}]})
    assert r.status_code == 200
    res = r.json()["results"]["q1"]
    assert res["score"] == 0 and len(res["missing"]) == 3
    assert r.json()["usage"]["marks_used"] == 0


def test_mark_theory_invalid_json_twice_is_friendly_and_refunded():
    p = Scripted(["garbage"])
    c = make_client(p)
    r = c.post("/api/exam/mark", json={"items": [{"id": "q1", "question": theory_q(), "answer": "Some answer here"}]})
    assert r.status_code == 502 and "try marking again" in r.json()["detail"]
    assert len(p.calls) == 2
    assert c.get("/api/usage").json()["marks_used"] == 0


def test_mark_rejects_bad_questions_and_enforces_limit():
    c = make_client(free_exam_marks_per_day=1, guest_exam_marks_per_day=1)
    assert c.post("/api/exam/mark", json={"items": [{"id": "q1", "question": {"type": "theory", "question": "x"}, "answer": "y"}]}).status_code == 422
    assert c.post("/api/exam/mark", json={"items": [{"id": "q1", "question": {"type": "essay"}}]}).status_code == 422
    body = {"items": [{"id": "q1", "question": theory_q(), "answer": "glucose from carbon dioxide and water using light"}]}
    assert c.post("/api/exam/mark", json=body).status_code == 200
    assert c.post("/api/exam/mark", json=body).status_code == 429


def test_full_offline_flow_generate_then_mark():
    c = make_client()
    doc = upload(c)
    exam = c.post("/api/exam/generate", json={"doc_id": doc["id"], "qtype": "mixed", "count": 7, "theory_count": 2}).json()
    items = [{"id": q["id"], "question": q, "choice": q["answer"]} if q["type"] == "mcq"
             else {"id": q["id"], "question": q, "answer": q["model_answer"]} for q in exam["questions"]]
    r = c.post("/api/exam/mark", json={"items": items})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 7
    assert all(results[q["id"]]["correct"] for q in exam["questions"] if q["type"] == "mcq")
    assert all(0 <= results[q["id"]]["score"] <= results[q["id"]]["max"] for q in exam["questions"])


def test_exam_page_and_nav_links():
    c = make_client()
    r = c.get("/exam")
    assert r.status_code == 200 and "Exam Prep" in r.text
    assert 'href="/exam"' in c.get("/app").text
    landing = c.get("/").text
    assert 'href="/exam"' in landing and "Practice exam questions from your own notes" in landing
    assert c.get("/static/exam.js").status_code == 200
