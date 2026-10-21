"""One-click study and research tools. Each tool retrieves a tailored set of passages from the selected
documents, asks the model for strict JSON, validates it, and returns structured data plus a Markdown rendering.

Grounding rules shared by every tool: facts come only from the numbered sources, every claim carries [n],
and missing information is reported as missing instead of filled in from general knowledge."""
from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass, field
from typing import Callable

from .jsonutil import parse_json
from .providers import SHAPE_MARKER
from .rag import citations, embed_queries
from .store import Hit, VectorStore

MISSING = "Not stated in the document."

BASE_RULES = f"""You are DocuChat, a rigorous study and research assistant.
Grounding rules (mandatory):
- Use ONLY the numbered sources below for factual claims. Never add facts from outside knowledge, even if you know them.
- Cite the supporting source numbers in square brackets inside the text, e.g. "... grew 12% [3]" or "[1][4]".
- If the sources do not contain something you are asked for, write "{MISSING}" for that field instead of guessing.
- Write in clear, plain English. Markdown is allowed inside string values; LaTeX math as $...$.
- Output strict JSON only: no commentary, no code fences, double-quoted keys and strings."""


class ToolInputError(ValueError):
    """The request can't be served as asked (bad options, wrong number of documents)."""


class ToolOutputError(RuntimeError):
    """The model's reply could not be turned into the expected structure, even after a retry."""


# ---------- small coercion helpers ----------

def s(value, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(s(v, limit) for v in value)
    elif isinstance(value, dict):
        value = "; ".join(f"{k}: {s(v, limit)}" for k, v in value.items())
    text = str(value).strip()
    return text[:limit]


def str_list(value, limit_items: int = 30, limit_chars: int = 800) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [s(v, limit_chars) for v in value]
    return [v for v in out if v][:limit_items]


def strip_cites(text: str) -> str:
    return re.sub(r"\s*\[\d+\]", "", text or "").strip()


# ---------- context gathering ----------

def gather(provider, store: VectorStore, doc_ids: list[str], queries: list[str], *, first: int = 1,
           spread: int = 6, per_query: int = 2, char_budget: int = 14000) -> list[Hit]:
    """Pick passages per document: the opening chunk(s) (title/abstract), the best matches for each tool-specific
    query, then evenly spread chunks for coverage. Each document gets an equal share of the character budget.
    Returned in reading order so the model sees a coherent document."""
    per_doc_budget = max(1500, char_budget // max(1, len(doc_ids)))
    qvecs = embed_queries(provider, queries) if queries else None
    picked: list[Hit] = []
    for doc_id in doc_ids:
        chunks = store.doc_chunks(doc_id)
        if not chunks:
            continue
        index = {id(c): i for i, c in enumerate(chunks)}
        order: list[int] = list(range(min(first, len(chunks))))
        if qvecs is not None:
            hit_lists = [store.search(q, per_query, [doc_id]) for q in qvecs]
            for rank in range(per_query):  # interleave so every query gets its best hit first
                for hits in hit_lists:
                    if rank < len(hits):
                        order.append(index[id(hits[rank].chunk)])
        if spread and len(chunks) > 1:
            step = len(chunks) / spread
            order.extend(min(len(chunks) - 1, int(i * step + step / 2)) for i in range(spread))
        seen, chosen, used = set(), [], 0
        for i in order:
            if i in seen:
                continue
            seen.add(i)
            size = len(chunks[i].text)
            if chosen and used + size > per_doc_budget:
                continue
            chosen.append(i)
            used += size
        picked.extend(Hit(chunk=chunks[i], score=1.0) for i in sorted(chosen))
    return picked


def sources_block(hits: list[Hit]) -> str:
    return "\n\n".join(f"[{i}] {h.chunk.filename}, page {h.chunk.page}\n{h.chunk.text}" for i, h in enumerate(hits, 1))


def docs_block(store: VectorStore, doc_ids: list[str]) -> str:
    lines = []
    for n, doc_id in enumerate(doc_ids, 1):
        info = store.info(doc_id) or {}
        meta = info.get("meta") or {}
        extra = ", ".join(f"{k}: {v}" for k, v in meta.items() if v)
        lines.append(f"Document {n}: {info.get('filename', doc_id)} ({info.get('pages', '?')} pages)"
                     + (f"; PDF metadata: {extra}" if extra else ""))
    return "\n".join(lines)


def complete_json(provider, system: str, user: str, normalize: Callable[[object], dict], max_tokens: int) -> dict:
    """Ask for JSON, parse robustly, validate; on failure retry once with a firmer instruction."""
    last_error: Exception | None = None
    prompt = user
    for _ in range(2):
        raw = provider.complete(system, prompt, json_mode=True, max_tokens=max_tokens)
        try:
            return normalize(parse_json(raw))
        except (ValueError, TypeError, KeyError) as exc:
            last_error = exc
            prompt = (user + "\n\nIMPORTANT: your previous reply was not valid JSON in the required shape. "
                      "Reply with ONLY the JSON object, nothing else.")
    raise ToolOutputError(f"The AI returned an unreadable response ({last_error}). Please try again.")


# ---------- tool definitions ----------

@dataclass
class Tool:
    name: str
    title: str
    task: str
    shape: dict
    normalize: Callable[[object, dict], dict]
    markdown: Callable[[dict], str]
    queries: Callable[[dict], list[str]] = lambda opts: []
    first: int = 1
    spread: int = 6
    per_query: int = 2
    char_budget: int = 14000
    max_tokens: int = 2048
    min_docs: int = 1
    per_doc: bool = False  # run once per selected document (charged per document)
    options: dict = field(default_factory=dict)

    def system(self, opts: dict) -> str:
        task = self.task.format(**opts)
        return f"{BASE_RULES}\n\nTask: {task}\n\n{SHAPE_MARKER}\n{json.dumps(self.shape, indent=1)}\n\n"


def _obj(data) -> dict:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


# Summarize
def norm_summary(data, opts):
    d = _obj(data)
    out = {"tldr": s(d.get("tldr")), "key_points": str_list(d.get("key_points"), 12), "takeaway": s(d.get("takeaway"), 600)}
    if not out["tldr"] and not out["key_points"]:
        raise ValueError("empty summary")
    return out


def md_summary(d):
    pts = "\n".join(f"- {p}" for p in d["key_points"])
    return f"**TL;DR:** {d['tldr']}\n\n**Key points**\n{pts}" + (f"\n\n**Takeaway:** {d['takeaway']}" if d["takeaway"] else "")


# Explain simply
def norm_explain(data, opts):
    d = _obj(data)
    terms = []
    for t in d.get("key_terms") or []:
        if isinstance(t, dict) and s(t.get("term")):
            terms.append({"term": s(t.get("term"), 120), "meaning": s(t.get("meaning"), 600)})
    out = {"title": s(d.get("title"), 200), "explanation": s(d.get("explanation"), 4000),
           "analogy": s(d.get("analogy"), 1000), "key_terms": terms[:8], "check_question": s(d.get("check_question"), 400)}
    if not out["explanation"]:
        raise ValueError("empty explanation")
    return out


def md_explain(d):
    parts = [f"### {d['title']}" if d["title"] else "", d["explanation"]]
    if d["analogy"]:
        parts.append(f"> **Analogy:** {d['analogy']}")
    if d["key_terms"]:
        parts.append("**Key terms**\n" + "\n".join(f"- **{t['term']}**: {t['meaning']}" for t in d["key_terms"]))
    if d["check_question"]:
        parts.append(f"**Check yourself:** {d['check_question']}")
    return "\n\n".join(p for p in parts if p)


# Flashcards
def norm_cards(data, opts):
    d = data if isinstance(data, list) else _obj(data).get("cards")
    cards = []
    for c in d or []:
        if isinstance(c, dict) and s(c.get("front")) and s(c.get("back")):
            cards.append({"front": s(c.get("front"), 400), "back": s(c.get("back"), 800)})
    if not cards:
        raise ValueError("no flashcards")
    return {"cards": cards[: opts.get("count", 12)]}


def md_cards(d):
    return "\n".join(f"{i}. **{c['front']}**\n   {c['back']}" for i, c in enumerate(d["cards"], 1))


# Quiz
def norm_quiz(data, opts):
    d = data if isinstance(data, list) else _obj(data).get("questions")
    out = []
    for q in d or []:
        if not isinstance(q, dict):
            continue
        options = str_list(q.get("options"), 6, 300)
        try:
            answer = int(q.get("answer", -1))
        except (TypeError, ValueError):
            answer = options.index(s(q.get("answer"))) if s(q.get("answer")) in options else -1
        if not s(q.get("question")) or len(options) < 2 or not 0 <= answer < len(options):
            continue
        # Models favour "A"; shuffle deterministically so the right answer's position is unbiased.
        correct = options[answer]
        rng = random.Random(hashlib.md5(s(q.get("question")).encode()).hexdigest())
        rng.shuffle(options)
        out.append({"question": s(q.get("question"), 600), "options": options, "answer": options.index(correct),
                    "explanation": s(q.get("explanation"), 800)})
    if not out:
        raise ValueError("no quiz questions")
    return {"questions": out[: opts.get("count", 8)]}


def md_quiz(d):
    rows = []
    for i, q in enumerate(d["questions"], 1):
        opts = "\n".join(f"   {'ABCDEF'[j]}. {o}" for j, o in enumerate(q["options"]))
        rows.append(f"{i}. {q['question']}\n{opts}\n   *Answer: {'ABCDEF'[q['answer']]}. {q['explanation']}*")
    return "\n\n".join(rows)


# Research brief
def norm_brief(data, opts):
    d = _obj(data)
    out = {"title": strip_cites(s(d.get("title"), 300)), "research_question": s(d.get("research_question")),
           "methodology": s(d.get("methodology"), 3000), "key_findings": str_list(d.get("key_findings"), 10),
           "limitations": str_list(d.get("limitations"), 8), "future_work": str_list(d.get("future_work"), 8),
           "contribution": s(d.get("contribution"), 800)}
    if not (out["research_question"] or out["methodology"] or out["key_findings"]):
        raise ValueError("empty brief")
    return out


def md_brief(d):
    def bl(items):
        return "\n".join(f"- {x}" for x in items) or f"- {MISSING}"
    return (f"### {d['title'] or 'Research brief'}\n\n**Research question:** {d['research_question'] or MISSING}\n\n"
            f"**Methodology:** {d['methodology'] or MISSING}\n\n**Key findings**\n{bl(d['key_findings'])}\n\n"
            f"**Limitations**\n{bl(d['limitations'])}\n\n**Future work**\n{bl(d['future_work'])}"
            + (f"\n\n**Main contribution:** {d['contribution']}" if d["contribution"] else ""))


# Compare
DEFAULT_ASPECTS = ["Research question / purpose", "Methodology", "Data or sample", "Key findings", "Limitations", "Conclusions"]


def norm_compare(data, opts):
    d = _obj(data)
    names = opts["doc_names"]
    rows = []
    for r in d.get("rows") or []:
        if not isinstance(r, dict) or not s(r.get("aspect")):
            continue
        cells = r.get("cells")
        if isinstance(cells, dict):
            cells = list(cells.values())
        cells = [s(c, 1200) for c in (cells or [])][: len(names)]
        cells += [MISSING] * (len(names) - len(cells))
        rows.append({"aspect": s(r.get("aspect"), 120), "cells": [c or MISSING for c in cells]})
    if not rows:
        raise ValueError("no comparison rows")
    return {"documents": names, "rows": rows[:12], "summary": s(d.get("summary"), 2000)}


def md_compare(d):
    esc = lambda t: t.replace("|", "\\|").replace("\n", " ")
    head = "| Aspect | " + " | ".join(esc(n) for n in d["documents"]) + " |\n|---|" + "---|" * len(d["documents"])
    body = "\n".join(f"| **{esc(r['aspect'])}** | " + " | ".join(esc(c) for c in r["cells"]) + " |" for r in d["rows"])
    return f"{head}\n{body}" + (f"\n\n{d['summary']}" if d["summary"] else "")


# Cite this document
def norm_cite(data, opts):
    d = _obj(data)
    authors = []
    for a in d.get("authors") or []:
        if isinstance(a, dict) and (s(a.get("family")) or s(a.get("given"))):
            authors.append({"given": strip_cites(s(a.get("given"), 80)), "family": strip_cites(s(a.get("family"), 80))})
        elif isinstance(a, str) and a.strip():
            parts = strip_cites(a).rsplit(" ", 1)
            authors.append({"given": parts[0] if len(parts) == 2 else "", "family": parts[-1]})
    fields = {k: strip_cites(s(d.get(k), 300)) for k in ("type", "title", "year", "venue", "volume", "issue", "pages", "publisher", "doi", "url")}
    fields["year"] = (re.search(r"\b(1[5-9]\d\d|20\d\d)\b", fields["year"]) or [""])[0]
    fields["doi"] = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", fields["doi"], flags=re.I)
    if fields["type"] not in ("article", "inproceedings", "book", "report", "thesis", "misc"):
        fields["type"] = "article" if fields["venue"] else "misc"
    if fields["title"].lower() == MISSING.lower():
        fields["title"] = ""
    for k, v in list(fields.items()):
        if v.lower().startswith("not stated"):
            fields[k] = ""
    fields["authors"] = authors[:30]
    if not fields["title"]:
        fields["title"] = opts.get("fallback_title", "")
    return fields


def _initials(given: str) -> str:
    return " ".join(f"{p[0]}." for p in re.split(r"[\s.]+", given) if p)


def format_apa(c: dict) -> str:
    names = [f"{a['family']}, {_initials(a['given'])}".strip(", ") for a in c["authors"]]
    if len(names) > 20:
        names = names[:19] + ["…", names[-1]]
    authors = names[0] if len(names) == 1 else (", ".join(names[:-1]) + ", & " + names[-1] if names else "")
    year = f"({c['year'] or 'n.d.'})."
    head = f"{authors} {year}" if authors else f"{c['title']}. {year}"
    title = f" {c['title']}." if authors and c["title"] else ""
    src = c["venue"]
    if src and c["volume"]:
        src += f", {c['volume']}" + (f"({c['issue']})" if c["issue"] else "")
    if src and c["pages"]:
        src += f", {c['pages']}"
    if not src and c["publisher"]:
        src = c["publisher"]
    link = f" https://doi.org/{c['doi']}" if c["doi"] else (f" {c['url']}" if c["url"] else "")
    return re.sub(r"\s+", " ", f"{head}{title}" + (f" {src}." if src else "") + link).strip()


def format_mla(c: dict) -> str:
    a = c["authors"]
    if not a:
        authors = ""
    elif len(a) == 1:
        authors = f"{a[0]['family']}, {a[0]['given']}".strip(", ")
    elif len(a) == 2:
        authors = f"{a[0]['family']}, {a[0]['given']}, and {a[1]['given']} {a[1]['family']}".replace(" ,", ",")
    else:
        authors = f"{a[0]['family']}, {a[0]['given']}, et al".strip(", ")
    parts = [f"{authors}." if authors else "", f"\u201c{c['title']}.\u201d" if c["title"] else ""]
    container = [c["venue"]] if c["venue"] else []
    if c["volume"]:
        container.append(f"vol. {c['volume']}")
    if c["issue"]:
        container.append(f"no. {c['issue']}")
    if c["publisher"] and not c["venue"]:
        container.append(c["publisher"])
    if c["year"]:
        container.append(c["year"])
    if c["pages"]:
        container.append(f"pp. {c['pages']}")
    if c["doi"]:
        container.append(f"https://doi.org/{c['doi']}")
    elif c["url"]:
        container.append(c["url"])
    if container:
        parts.append(", ".join(container) + ".")
    return re.sub(r"\.\.", ".", " ".join(p for p in parts if p)).strip()


def format_bibtex(c: dict) -> str:
    fam = re.sub(r"[^a-z]", "", (c["authors"][0]["family"].lower() if c["authors"] else "anon")) or "anon"
    word = next((w for w in re.findall(r"[a-z]+", c["title"].lower()) if len(w) > 3), "doc")
    key = f"{fam}{c['year'] or 'nd'}{word}"
    entry = {"inproceedings": "inproceedings", "book": "book", "report": "techreport", "thesis": "phdthesis",
             "article": "article"}.get(c["type"], "misc")
    clean = lambda v: v.replace("{", "").replace("}", "")
    fields = [("author", " and ".join(f"{clean(a['family'])}, {clean(a['given'])}".strip(", ") for a in c["authors"])),
              ("title", "{" + clean(c["title"]) + "}" if c["title"] else ""),
              ("booktitle" if entry == "inproceedings" else "journal", clean(c["venue"]) if entry in ("article", "inproceedings") else ""),
              ("publisher", clean(c["publisher"])), ("year", c["year"]), ("volume", c["volume"]), ("number", c["issue"]),
              ("pages", c["pages"].replace("-", "--").replace("----", "--")), ("doi", c["doi"]), ("url", c["url"])]
    body = ",\n".join(f"  {k} = {{{v}}}" for k, v in fields if v)
    return f"@{entry}{{{key},\n{body}\n}}"


def md_cite(d):
    return f"**APA:** {d['formatted']['apa']}\n\n**MLA:** {d['formatted']['mla']}\n\n```bibtex\n{d['formatted']['bibtex']}\n```"


TOOLS: dict[str, Tool] = {
    "summarize": Tool(
        name="summarize", title="Summary",
        task="Summarize the document(s): a 2-3 sentence TL;DR, then 5-8 key points covering the whole document in order, "
             "then a one-line takeaway. If several documents are in scope, cover each and name it.",
        shape={"tldr": "<2-3 sentence TL;DR with [n] citations>", "key_points": ["<key point with [n]>"],
               "takeaway": "<one-line takeaway with [n]>"},
        normalize=norm_summary, markdown=md_summary,
        queries=lambda o: ["main purpose, thesis and conclusion", "most important results, arguments and evidence"],
        first=2, spread=10, char_budget=16000),
    "explain": Tool(
        name="explain", title="Explained simply",
        task="Explain {topic_or_main} in simple language a 15-year-old could follow. Short sentences, no jargon "
             "(define any term you must use). An analogy is welcome but must be labelled as an analogy and must not "
             "introduce new facts. If the sources do not cover the topic, say so in the explanation.",
        shape={"title": "<what is being explained>", "explanation": "<simple Markdown explanation with [n]>",
               "analogy": "<everyday analogy, or empty string>",
               "key_terms": [{"term": "<term from the sources>", "meaning": "<plain meaning with [n]>"}],
               "check_question": "<one question to check understanding>"},
        normalize=norm_explain, markdown=md_explain,
        queries=lambda o: [o["topic"]] if o.get("topic") else ["main idea and core concepts", "definitions of key terms"],
        per_query=4, spread=4, char_budget=10000),
    "flashcards": Tool(
        name="flashcards", title="Flashcards",
        task="Create exactly {count} high-quality study flashcards covering the most important facts, definitions, "
             "and ideas across the whole document. Front: a clear question or term. Back: a concise answer (1-2 "
             "sentences) with [n]. No duplicates, no trivia about formatting.",
        shape={"cards": [{"front": "<question or term>", "back": "<concise answer with [n]>"}]},
        normalize=norm_cards, markdown=md_cards,
        queries=lambda o: [o["topic"]] if o.get("topic") else ["key definitions and concepts", "important facts and results"],
        first=1, spread=12, char_budget=16000, max_tokens=3072),
    "quiz": Tool(
        name="quiz", title="Quiz",
        task="Write exactly {count} {difficulty} multiple-choice questions that test understanding of the document "
             "(not trivia). Each has 4 plausible options, exactly one correct; 'answer' is the 0-based index of the "
             "correct option. The explanation says why it is correct, with [n].",
        shape={"questions": [{"question": "<question>", "options": ["<option>", "<option>", "<option>", "<option>"],
                              "answer": 0, "explanation": "<why, with [n]>"}]},
        normalize=norm_quiz, markdown=md_quiz,
        queries=lambda o: [o["topic"]] if o.get("topic") else ["key concepts and their relationships", "main results and conclusions"],
        first=1, spread=12, char_budget=16000, max_tokens=3072),
    "brief": Tool(
        name="brief", title="Research brief",
        task="Write a research brief of this paper: its research question, methodology (design, data/sample, "
             "methods, evaluation), key findings with numbers where given, limitations (stated by the authors or "
             "evident from the methods as described), future work, and its main contribution. Use \"" + MISSING + "\" "
             "for anything the paper does not cover.",
        shape={"title": "<paper title as written>", "research_question": "<with [n]>", "methodology": "<with [n]>",
               "key_findings": ["<finding with [n]>"], "limitations": ["<limitation with [n]>"],
               "future_work": ["<item with [n]>"], "contribution": "<one sentence with [n]>"},
        normalize=norm_brief, markdown=md_brief,
        queries=lambda o: ["research question, objective, aim, hypothesis", "methodology, methods, data, participants, experimental setup",
                           "results and key findings", "limitations and threats to validity", "future work and further research", "conclusion and contribution"],
        first=2, spread=3, per_query=2, char_budget=16000, per_doc=True),
    "compare": Tool(
        name="compare", title="Comparison",
        task="Compare the documents side by side on these aspects: {aspects}. Documents in column order: {doc_order}. "
             "'cells' has exactly one entry per document in that order; each cell states what THAT document says, "
             "with [n] pointing to that document's sources, or \"" + MISSING + "\". Then a 2-3 sentence summary of "
             "the main agreements and differences, with [n].",
        shape={"rows": [{"aspect": "<aspect>", "cells": ["<what document 1 says, with [n]>", "<document 2 ...>"]}],
               "summary": "<agreements and differences, with [n]>"},
        normalize=norm_compare, markdown=md_compare,
        queries=lambda o: o["aspects_list"],
        first=1, spread=2, per_query=2, char_budget=18000, min_docs=2, max_tokens=3072),
    "cite": Tool(
        name="cite", title="Citation",
        task="Extract bibliographic details for THIS document from its title page, header/footer text and PDF "
             "metadata only. Do not guess: leave a field as an empty string when it is not printed in the sources. "
             "type is one of article, inproceedings, book, report, thesis, misc. year is 4 digits. No [n] markers.",
        shape={"type": "article", "title": "<full title>", "authors": [{"given": "<given names>", "family": "<family name>"}],
               "year": "<YYYY>", "venue": "<journal or conference>", "volume": "", "issue": "", "pages": "",
               "publisher": "", "doi": "", "url": ""},
        normalize=norm_cite, markdown=md_cite,
        first=4, spread=0, char_budget=6000, per_doc=True, max_tokens=1024),
}


def resolve_options(tool: Tool, opts: dict, store: VectorStore, doc_ids: list[str]) -> dict:
    topic = s(opts.get("topic"), 500)
    count = int(opts.get("count") or (12 if tool.name == "flashcards" else 8))
    out = {"topic": topic, "topic_or_main": f'"{topic}"' if topic else "the main ideas of the document(s)",
           "count": max(3, min(count, 30 if tool.name == "flashcards" else 20)),
           "difficulty": opts.get("difficulty") if opts.get("difficulty") in ("easy", "medium", "hard") else "medium"}
    if tool.name == "compare":
        aspects = [a.strip() for a in re.split(r"[,;\n]", topic) if a.strip()][:8] if topic else DEFAULT_ASPECTS
        names = [(store.info(d) or {}).get("filename", d) for d in doc_ids]
        out.update(aspects=", ".join(aspects), aspects_list=aspects, doc_names=names,
                   doc_order="; ".join(f"{i}. {n}" for i, n in enumerate(names, 1)))
    return out


def cost(tool_name: str, doc_ids: list[str]) -> int:
    tool = TOOLS[tool_name]
    return len(doc_ids) if tool.per_doc else 1


def run_tool(provider, store: VectorStore, name: str, doc_ids: list[str], opts: dict | None = None) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolInputError(f"Unknown tool '{name}'")
    if len(doc_ids) < tool.min_docs:
        raise ToolInputError(f"{tool.title} needs at least {tool.min_docs} documents selected.")
    o = resolve_options(tool, opts or {}, store, doc_ids)
    groups = [[d] for d in doc_ids] if tool.per_doc else [doc_ids]
    items, all_sources, md_parts = [], [], []
    for group in groups:
        hits = gather(provider, store, group, tool.queries(o), first=tool.first, spread=tool.spread,
                      per_query=tool.per_query, char_budget=tool.char_budget)
        if not hits:
            raise ToolInputError("Those documents have no indexed text.")
        info = store.info(group[0]) or {}
        o["fallback_title"] = (info.get("meta") or {}).get("title", "")
        user = f"{docs_block(store, group)}\n\nSources:\n\n{sources_block(hits)}\n\nTask: {tool.title} (follow the system instructions)."
        data = complete_json(provider, tool.system(o), user, lambda d: tool.normalize(d, o), tool.max_tokens)
        srcs = citations(hits)
        if tool.name == "cite":
            data["formatted"] = {"apa": format_apa(data), "mla": format_mla(data), "bibtex": format_bibtex(data)}
        if tool.per_doc:
            data = {"doc_id": group[0], "filename": info.get("filename", ""), **data, "sources": srcs}
            items.append(data)
            md_parts.append((f"#### {info.get('filename', '')}\n\n" if len(groups) > 1 else "") + tool.markdown(data))
        else:
            items.append(data)
            md_parts.append(tool.markdown(data))
        all_sources.append(srcs)
    if tool.per_doc:
        return {"tool": name, "title": tool.title, "data": {"items": items}, "markdown": "\n\n---\n\n".join(md_parts),
                "sources": all_sources[0] if len(all_sources) == 1 else []}
    return {"tool": name, "title": tool.title, "data": items[0], "markdown": md_parts[0], "sources": all_sources[0]}
