import json


def read_stream(resp):
    return [json.loads(line) for line in resp.text.splitlines() if line]


def upload(client, pdf, name="handbook.pdf"):
    return client.post("/api/documents", files={"file": (name, pdf, "application/pdf")})


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_upload_list_delete(client, handbook_pdf):
    r = upload(client, handbook_pdf)
    assert r.status_code == 201
    doc = r.json()
    assert doc["pages"] == 2
    assert [d["id"] for d in client.get("/api/documents").json()] == [doc["id"]]
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 200
    assert client.get("/api/documents").json() == []
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 404


def test_rejects_non_pdf(client):
    r = client.post("/api/documents", files={"file": ("notes.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_chat_cites_the_right_page(client, handbook_pdf):
    upload(client, handbook_pdf)
    events = read_stream(client.post("/api/chat", json={"question": "How many days can employees work remotely per week?"}))
    assert events[0]["type"] == "sources"
    assert events[0]["sources"][0]["page"] == 2
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert "three days" in answer
    assert events[-1]["type"] == "done"


def test_chat_without_documents_refuses(client):
    events = read_stream(client.post("/api/chat", json={"question": "What is the leave policy?"}))
    assert events[0]["sources"] == []
    answer = "".join(e["text"] for e in events if e["type"] == "token")
    assert "couldn't find" in answer


def test_workspaces_are_isolated(client, handbook_pdf):
    upload(client, handbook_pdf)
    from fastapi.testclient import TestClient
    other = TestClient(client.app)
    assert other.get("/api/documents").json() == []
    assert len(client.get("/api/documents").json()) == 1


def test_daily_question_limit(handbook_pdf):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.providers import FakeProvider
    c = TestClient(create_app(Settings(provider="fake", free_questions_per_day=2), provider=FakeProvider()))
    for _ in range(2):
        assert c.post("/api/chat", json={"question": "leave?"}).status_code == 200
    r = c.post("/api/chat", json={"question": "leave?"})
    assert r.status_code == 429
    assert c.get("/api/usage").json()["questions_used"] == 2


def test_document_limit(handbook_pdf):
    from fastapi.testclient import TestClient
    from app.config import Settings
    from app.main import create_app
    from app.providers import FakeProvider
    c = TestClient(create_app(Settings(provider="fake", free_docs_per_workspace=1), provider=FakeProvider()))
    assert upload(c, handbook_pdf).status_code == 201
    assert upload(c, handbook_pdf).status_code == 402
