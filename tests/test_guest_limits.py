from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.providers import FakeProvider


def guest(**kw):
    s = Settings(provider="fake", min_score=0.05, free_questions_per_day=20, guest_questions_per_day=2,
                 free_docs_per_workspace=3, guest_docs_per_workspace=1, guest_exam_sets_per_day=1, **kw)
    return TestClient(create_app(s, provider=FakeProvider()))


def test_guest_usage_reports_guest_limits(handbook_pdf):
    c = guest()
    u = c.get("/api/usage").json()
    assert u["signed_in"] is False and u["questions_limit"] == 2 and u["docs_limit"] == 1 and u["exams_limit"] == 1
    assert u["account_limits"]["questions"] == 20
    assert c.get("/api/config").json()["guest_limits"]["questions"] == 2


def test_guest_hits_limits_with_signup_pitch(handbook_pdf):
    c = guest()
    r = c.post("/api/documents", files={"file": ("a.pdf", handbook_pdf, "application/pdf")})
    assert r.status_code == 201
    d = r.json()["id"]
    r2 = c.post("/api/documents", files={"file": ("b.pdf", handbook_pdf, "application/pdf")})
    assert r2.status_code == 402 and "free account" in r2.json()["detail"]
    for _ in range(2):
        assert c.post("/api/chat", json={"question": "What is this about?", "doc_ids": [d]}).status_code == 200
    r3 = c.post("/api/chat", json={"question": "More?", "doc_ids": [d]})
    assert r3.status_code == 429 and "free account" in r3.json()["detail"]


def test_new_guest_on_same_ip_is_not_blocked_by_another(handbook_pdf):
    # Campus Wi-Fi: one guest using up their allowance must not block the next guest on the same IP.
    a, b = guest(), None
    app = a.app
    b = TestClient(app)
    d = a.post("/api/documents", files={"file": ("a.pdf", handbook_pdf, "application/pdf")}).json()["id"]
    for _ in range(2):
        a.post("/api/chat", json={"question": "q", "doc_ids": [d]})
    assert a.post("/api/chat", json={"question": "q", "doc_ids": [d]}).status_code == 429
    d2 = b.post("/api/documents", files={"file": ("b.pdf", handbook_pdf, "application/pdf")}).json()["id"]
    assert b.post("/api/chat", json={"question": "q", "doc_ids": [d2]}).status_code == 200
