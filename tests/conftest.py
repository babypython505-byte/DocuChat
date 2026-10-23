import os

# Existing tests exercise the plan limits through guest clients; give guests the account limits by default here.
# tests/test_guest_limits.py covers the real (smaller) guest allowance explicitly.
for _k, _v in {"GUEST_QUESTIONS_PER_DAY": "20", "GUEST_DOCS_PER_WORKSPACE": "3",
               "GUEST_EXAM_SETS_PER_DAY": "5", "GUEST_EXAM_MARKS_PER_DAY": "40"}.items():
    os.environ.setdefault(_k, _v)

import io

import pytest
from fastapi.testclient import TestClient
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.config import Settings
from app.main import create_app
from app.providers import FakeProvider


def make_pdf(pages: list[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for text in pages:
        y = 740
        for line in text.split("\n"):
            c.drawString(60, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def client():
    settings = Settings(provider="fake", min_score=0.05)
    return TestClient(create_app(settings, provider=FakeProvider()))


@pytest.fixture
def handbook_pdf():
    return make_pdf([
        "Employee Handbook\nAll staff get 25 days of paid annual leave per year.",
        "Remote work policy\nEmployees may work remotely up to three days per week.",
    ])
