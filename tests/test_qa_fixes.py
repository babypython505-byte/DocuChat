"""Regression tests for bugs found in the v2 end-to-end browser QA."""
import re
from pathlib import Path

from tests.conftest import make_pdf
from tests.test_features import read_stream, upload

STATIC = Path(__file__).resolve().parent.parent / "static"


def lecture_pdf():
    return make_pdf([
        "BIO201 Lecture 7\nOsmosis is the net movement of water across a selectively permeable membrane.",
        "Active transport\nThe sodium potassium pump uses ATP to move ions against their gradient.",
    ])


def test_chat_stream_is_not_no_store(client):
    """'no-store' on a progressively-read NDJSON stream made Chromium report every answer as net::ERR_ABORTED."""
    doc = upload(client, lecture_pdf(), "bio.pdf")
    r = client.post("/api/chat", json={"question": "What is osmosis?", "doc_ids": [doc["id"]]})
    assert r.status_code == 200
    cc = r.headers["cache-control"]
    assert "no-store" not in cc and "no-cache" in cc and "no-transform" in cc
    assert r.headers["x-accel-buffering"] == "no"
    assert read_stream(r)[-1]["type"] == "done"


def test_other_api_responses_stay_no_store(client):
    assert client.get("/api/usage").headers["cache-control"] == "no-store"
    assert client.get("/api/documents").headers["cache-control"] == "no-store"


def test_server_wipe_then_silent_restore(client):
    """Simulates a Render restart: memory is cleared, the browser's cookie survives, IndexedDB restores the doc."""
    doc = upload(client, lecture_pdf(), "bio.pdf")
    used_before = client.get("/api/usage").json()["questions_used"]

    client.app.state.workspaces.clear()

    r = client.post("/api/chat", json={"question": "What is osmosis?", "doc_ids": [doc["id"]]})
    assert r.status_code == 409
    body = r.json()
    assert body["code"] == "missing_documents" and body["missing"] == [doc["id"]]
    assert client.get("/api/usage").json()["questions_used"] == used_before  # a 409 costs no question
    assert client.get("/api/documents").json() == []

    idx = doc["index"]
    payload = {"id": doc["id"], "filename": doc["filename"], "pages": doc["pages"], "meta": doc["meta"],
               "dim": idx["dim"], "dtype": idx["dtype"], "chunks": idx["chunks"], "vectors": idx["vectors"]}
    r = client.post("/api/documents/restore", json=payload)
    assert r.status_code == 200 and r.json()["restored"] is True
    assert [d["id"] for d in client.get("/api/documents").json()] == [doc["id"]]

    r = client.post("/api/chat", json={"question": "What is osmosis?", "doc_ids": [doc["id"]]})
    assert r.status_code == 200
    events = read_stream(r)
    assert events[0]["type"] == "sources" and events[0]["sources"][0]["doc_id"] == doc["id"]
    assert events[0]["sources"][0]["page"] == 1

    # tools work after the restore too
    r = client.post("/api/tools/summarize", json={"doc_ids": [doc["id"]]})
    assert r.status_code == 200 and r.json()["tool"] == "summarize"


def test_workspaces_clear_drops_everything(client):
    upload(client, lecture_pdf(), "bio.pdf")
    ws = client.app.state.workspaces
    assert len(ws._items) == 1
    ws.clear()
    assert len(ws._items) == 0


def test_app_js_has_no_inline_handlers_and_html_has_no_inline_scripts():
    """CSP is script-src 'self' + jsDelivr: inline <script> blocks or on*= attributes would be blocked."""
    html = (STATIC / "app.html").read_text()
    for tag in re.findall(r"<script\b[^>]*>", html):
        assert "src=" in tag, tag
    assert not re.search(r"\son[a-z]+\s*=", html, re.I)


def test_compare_table_cells_do_not_break_mid_word():
    css = (STATIC / "app.css").read_text()
    assert re.search(r"\.md th, \.md td \{[^}]*overflow-wrap: break-word", css)
