import pytest

from app.ingest import chunk_pdf, split_text
from tests.conftest import make_pdf


def test_split_respects_size_and_overlap():
    text = ". ".join(f"Sentence number {i}" for i in range(200))
    chunks = split_text(text, size=200, overlap=40)
    assert len(chunks) > 5
    assert all(len(c) <= 200 for c in chunks)


def test_split_empty_text():
    assert split_text("   ", 100, 10) == []


def test_overlap_must_be_smaller_than_size():
    with pytest.raises(ValueError):
        split_text("hello", 10, 10)


def test_chunk_pdf_keeps_page_numbers():
    pdf = make_pdf(["first page text", "second page text"])
    chunks = chunk_pdf("d1", "x.pdf", pdf, 500, 50)
    assert [c.page for c in chunks] == [1, 2]
    assert chunks[0].filename == "x.pdf"
