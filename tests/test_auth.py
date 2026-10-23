"""Supabase token verification (network mocked) and signed-in identity."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import TokenVerifier, bearer
from app.config import Settings
from app.main import create_app
from app.providers import FakeProvider

from .conftest import make_pdf

TOKEN_A = "aaa.bbb.ccc"
TOKEN_B = "ddd.eee.fff"
USERS = {TOKEN_A: {"id": "11111111-1111-1111-1111-111111111111", "email": "ada@example.com"},
         TOKEN_B: {"id": "22222222-2222-2222-2222-222222222222", "email": "bola@example.com"}}


class FakeFetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, token):
        self.calls += 1
        return USERS.get(token)


def make(fetch=None, ttl=60):
    fetch = fetch or FakeFetch()
    return TokenVerifier("https://proj.supabase.co", "sb_publishable_x", ttl=ttl, fetch=fetch), fetch


def test_bearer_parsing():
    assert bearer("Bearer abc.def.ghi") == "abc.def.ghi"
    assert bearer("bearer  x.y.z ") == "x.y.z"
    assert bearer("Basic abc") is None and bearer(None) is None and bearer("Bearer ") is None


def test_verify_valid_invalid_and_cache():
    v, fetch = make()
    u = v.verify(TOKEN_A)
    assert u.id == USERS[TOKEN_A]["id"] and u.email == "ada@example.com"
    assert v.verify(TOKEN_A) == u and fetch.calls == 1          # cached
    assert v.verify("bad.token.here") is None
    assert v.verify("bad.token.here") is None and fetch.calls == 2  # negative result cached too
    assert v.verify("not-a-jwt") is None and v.verify(None) is None and fetch.calls == 2  # no network for junk


def test_cache_expires():
    v, fetch = make(ttl=0)
    v.verify(TOKEN_A); v.verify(TOKEN_A)
    assert fetch.calls == 2


def test_network_error_is_guest_and_not_cached():
    calls = []

    def boom(token):
        calls.append(token)
        raise httpx.ConnectError("down")
    v, _ = make(fetch=boom)
    assert v.verify(TOKEN_A) is None and v.verify(TOKEN_A) is None and len(calls) == 2


def test_disabled_without_config():
    v = TokenVerifier("", "", fetch=lambda t: USERS[TOKEN_A])
    assert not v.enabled and v.verify(TOKEN_A) is None


def test_real_fetch_sends_apikey_and_bearer(monkeypatch):
    seen = {}

    def fake_get(url, headers, timeout):
        seen.update(url=url, headers=headers)
        if headers["Authorization"] == f"Bearer {TOKEN_A}":
            return httpx.Response(200, json=USERS[TOKEN_A], request=httpx.Request("GET", url))
        return httpx.Response(401, json={"msg": "invalid JWT"}, request=httpx.Request("GET", url))
    monkeypatch.setattr(httpx, "get", fake_get)
    v = TokenVerifier("https://proj.supabase.co/", "sb_publishable_x")
    assert v.verify(TOKEN_A).email == "ada@example.com"
    assert seen["url"] == "https://proj.supabase.co/auth/v1/user" and seen["headers"]["apikey"] == "sb_publishable_x"
    assert v.verify(TOKEN_B) is None


@pytest.fixture
def auth_client():
    v, _ = make()
    return TestClient(create_app(Settings(provider="fake", min_score=0.05), provider=FakeProvider(), verifier=v))


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_me_and_config(auth_client):
    assert auth_client.get("/api/me").json()["signed_in"] is False
    me = auth_client.get("/api/me", headers=auth(TOKEN_A)).json()
    assert me == {"signed_in": True, "id": USERS[TOKEN_A]["id"], "email": "ada@example.com"}
    cfg = auth_client.get("/api/config").json()["auth"]
    assert cfg["enabled"] and cfg["supabase_url"].startswith("https://") and "service" not in str(cfg).lower()


def test_documents_follow_the_user_not_the_cookie(auth_client):
    pdf = make_pdf(["Photosynthesis makes glucose from light."])
    r = auth_client.post("/api/documents", files={"file": ("bio.pdf", pdf, "application/pdf")}, headers=auth(TOKEN_A))
    assert r.status_code == 201 and r.json()["usage"]["signed_in"] is True
    doc_id = r.json()["id"]
    # a different browser (fresh cookie jar) signed in as the same user sees the document
    other = TestClient(auth_client.app)
    assert [d["id"] for d in other.get("/api/documents", headers=auth(TOKEN_A)).json()] == [doc_id]
    # the same browser as a guest, or as another user, does not
    assert auth_client.get("/api/documents").json() == []
    assert auth_client.get("/api/documents", headers=auth(TOKEN_B)).json() == []


def test_bad_token_falls_back_to_guest(auth_client):
    r = auth_client.get("/api/usage", headers=auth("bad.token.here"))
    assert r.status_code == 200 and r.json()["signed_in"] is False


def test_guest_cannot_claim_user_workspace(auth_client):
    auth_client.get("/api/usage", headers=auth(TOKEN_A))
    guest = TestClient(auth_client.app, cookies={"dc_ws": f"user:{USERS[TOKEN_A]['id']}"})
    assert guest.get("/api/usage").json()["signed_in"] is False


def test_quota_keyed_by_user(auth_client):
    pdf = make_pdf(["Water boils at 100 degrees Celsius at sea level."])
    doc = auth_client.post("/api/documents", files={"file": ("w.pdf", pdf, "application/pdf")}, headers=auth(TOKEN_A)).json()
    r = auth_client.post("/api/chat", json={"question": "When does water boil?", "doc_ids": [doc["id"]]}, headers=auth(TOKEN_A))
    assert r.status_code == 200
    other = TestClient(auth_client.app)
    assert other.get("/api/usage", headers=auth(TOKEN_A)).json()["questions_used"] >= 1


def test_login_page_exam_redirect_and_csp(auth_client):
    r = auth_client.get("/login")
    assert r.status_code == 200 and "auth.js" in r.text
    r = auth_client.get("/exam", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/app?mode=exam"
    assert auth_client.get("/exam?resume=1", follow_redirects=False).headers["location"] == "/app?mode=exam&resume=1"
    assert "https://proj.supabase.co" in auth_client.get("/app").headers["content-security-policy"]
