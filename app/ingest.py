"""PDF parsing and chunking. Each chunk keeps its source file and page number for citations."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from pypdf import PdfReader


@dataclass
class Chunk:
    doc_id: str
    filename: str
    page: int
    text: str


def extract_pages(data: bytes) -> list[str]:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text() or ""
        pages.append(re.sub(r"[ \t]+", " ", text).strip())
    return pages


def split_text(text: str, size: int, overlap: int) -> list[str]:
    """Split on paragraph/sentence boundaries where possible, falling back to hard cuts."""
    if overlap >= size:
        raise ValueError("overlap must be smaller than size")
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start:end]
            # prefer to break at a paragraph, then a sentence, then a space
            for sep in ("\n\n", ". ", "\n", " "):
                cut = window.rfind(sep)
                if cut > size * 0.5:
                    end = start + cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_pdf(doc_id: str, filename: str, data: bytes, size: int, overlap: int) -> list[Chunk]:
    out: list[Chunk] = []
    for page_no, page_text in enumerate(extract_pages(data), start=1):
        for piece in split_text(page_text, size, overlap):
            out.append(Chunk(doc_id=doc_id, filename=filename, page=page_no, text=piece))
    return out
