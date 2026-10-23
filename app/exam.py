"""Exam Prep: generate exam-style questions from a page range of one PDF, and mark the student's answers.

Generation reads the text of the chosen pages only (rebuilt from the stored chunks), asks the model for strict JSON
in batches, validates every question (options, answer index, page numbers inside the range, marks that add up) and
returns a clean exam set. MCQs are marked deterministically; theory answers are marked by the model against the
question's marking points, with partial credit, and the score is always recomputed and clamped on our side."""
from __future__ import annotations

import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from .jsonutil import parse_json
from .store import VectorStore

QTYPES = ("mcq", "theory", "mixed")
DIFFICULTIES = ("easy", "medium", "hard", "mixed")
STYLES = ("university", "waec", "jamb", "general")
STYLE_NAMES = {"university": "University", "waec": "WAEC (SSCE)", "jamb": "JAMB / UTME", "general": "General"}

MIN_QUESTIONS, MAX_QUESTIONS = 5, 50
MCQ_BATCH, THEORY_BATCH = 10, 4          # questions per model call; keeps each reply well under the output limit
CONTEXT_CHARS = 60000                    # page text sent per call (~15k tokens): fits the free tier comfortably
MAX_MARK_ITEMS = 10                      # theory answers marked in one request
MAX_ANSWER_CHARS = 6000

FRIENDLY_FAIL = ("The AI couldn't put this exam set together right now. Please try again in a minute, "
                 "or try fewer questions or a smaller page range.")
FRIENDLY_MARK_FAIL = "The AI couldn't mark this answer right now. Your answer is saved; please try marking again in a minute."

GEN_MARKER = "EXAM_MODE: generate"
MARK_MARKER = "EXAM_MODE: mark"


class ExamInputError(ValueError):
    """The request can't be served as asked (bad page range, no text on those pages)."""


class ExamOutputError(RuntimeError):
    """The model's reply could not be turned into valid questions or marks, even after a retry."""


# ---------- helpers ----------

def _s(value, limit: int = 2000) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = " ".join(_s(v, limit) for v in value)
    elif isinstance(value, dict):
        value = " ".join(_s(v, limit) for v in value.values())
    return str(value).strip()[:limit]


def _str_list(value, items: int = 10, chars: int = 500) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [_s(v, chars) for v in value]
    return [v for v in out if v][:items]


def _int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(round(value))
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return int(round(float(m.group(0)))) if m else default


def _num(value, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value or ""))
    return float(m.group(0)) if m else default


def _norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def _words(t: str) -> set[str]:
    return {w for w in _norm_text(t).split() if len(w) > 3}


# ---------- page text ----------

def _merge_overlap(a: str, b: str, max_overlap: int = 600) -> str:
    """Join two consecutive chunks of one page, dropping the text they share (the chunker overlaps them)."""
    if not a:
        return b
    for k in range(min(len(a), len(b), max_overlap), 11, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return a + "\n" + b


def page_texts(store: VectorStore, doc_id: str) -> dict[int, str]:
    pages: dict[int, str] = {}
    for c in store.doc_chunks(doc_id):
        pages[c.page] = _merge_overlap(pages.get(c.page, ""), c.text)
    return pages


def select_pages(pages: dict[int, str], page_from: int, page_to: int) -> dict[int, str]:
    return {p: t for p, t in sorted(pages.items()) if page_from <= p <= page_to and t.strip()}


def fit_context(pages: dict[int, str], topic: str = "", budget: int = CONTEXT_CHARS) -> dict[int, str]:
    """Trim page text to the budget. Pages that mention the focus topic keep twice the share of the others."""
    total = sum(len(t) for t in pages.values())
    if total <= budget:
        return dict(pages)
    topic_words = _words(topic)
    weight = {p: (2.0 if topic_words and topic_words & _words(t) else 1.0) for p, t in pages.items()}
    unit = budget / max(1.0, sum(weight.values()))
    return {p: t[: max(300, int(unit * weight[p]))] for p, t in pages.items()}


def context_block(pages: dict[int, str]) -> str:
    return "\n\n".join(f"=== Page {p} ===\n{t}" for p, t in sorted(pages.items()))


def split_pages(pages: dict[int, str], parts: int) -> list[dict[int, str]]:
    """Split pages into up to `parts` contiguous groups of similar text size, so parallel batches cover different
    material and don't repeat each other. With too few pages, every batch sees all of them."""
    keys = sorted(pages)
    if parts <= 1 or len(keys) < parts * 2:
        return [dict(pages)] * max(1, parts)
    total = sum(len(pages[k]) for k in keys)
    groups, cur, size = [], {}, 0
    for k in keys:
        cur[k] = pages[k]
        size += len(pages[k])
        if size >= total * (len(groups) + 1) / parts and len(groups) < parts - 1:
            groups.append(cur)
            cur = {}
    if cur:
        groups.append(cur)
    while len(groups) < parts:
        groups.append(dict(pages))
    return groups


def best_page(text: str, pages: dict[int, str]) -> int | None:
    words = _words(text)
    if not pages:
        return None
    scored = sorted(((len(words & _words(t)), -p) for p, t in pages.items()), reverse=True)
    return -scored[0][1]


def parse_pages(value, lo: int, hi: int) -> list[int]:
    """Page refs from the model ("3", "p. 4-5", [2, "6"]) as sorted unique ints inside [lo, hi]."""
    items = value if isinstance(value, list) else [value]
    out: set[int] = set()
    for item in items:
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, (int, float)):
            out.add(int(item))
            continue
        for a, b in re.findall(r"(\d+)\s*(?:[-–]\s*(\d+))?", str(item)):
            a = int(a)
            b = int(b) if b else a
            if b < a or b - a > 20:
                b = a
            out.update(range(a, b + 1))
    return sorted(p for p in out if lo <= p <= hi)[:6]


# ---------- options ----------

def resolve_request(opts: dict, doc_pages: int) -> dict:
    qtype = opts.get("qtype") if opts.get("qtype") in QTYPES else "mcq"
    style = opts.get("style") if opts.get("style") in STYLES else "general"
    difficulty = opts.get("difficulty") if opts.get("difficulty") in DIFFICULTIES else "mixed"
    count = max(MIN_QUESTIONS, min(MAX_QUESTIONS, _int(opts.get("count"), 10)))
    lo = _int(opts.get("page_from"), 1) or 1
    hi = _int(opts.get("page_to"), doc_pages) or doc_pages
    lo, hi = max(1, lo), min(doc_pages, hi)
    if lo > hi:
        raise ExamInputError(f"The page range is empty. This PDF has {doc_pages} pages.")
    if qtype == "mcq":
        n_mcq, n_theory = count, 0
    elif qtype == "theory":
        n_mcq, n_theory = 0, count
    else:
        n_theory = _int(opts.get("theory_count"), 0) or max(1, round(count * 0.3))
        n_theory = max(1, min(count - 1, n_theory))
        n_mcq = count - n_theory
    return {"qtype": qtype, "style": style, "difficulty": difficulty, "count": count, "page_from": lo, "page_to": hi,
            "n_mcq": n_mcq, "n_theory": n_theory, "topic": _s(opts.get("topic"), 300),
            "weak_topics": _str_list(opts.get("weak_topics"), 6, 80), "missed": _str_list(opts.get("missed"), 12, 300)}


# ---------- prompts ----------

STYLE_GUIDE = {
    "university": {
        "mcq": "University course exam: 4 options (A-D), one best answer. Mostly understanding and application "
               "(scenarios, data, 'which best explains', 'what would happen if'), little pure recall.",
        "theory": "University exam theory: mix short-answer questions (5-10 marks) with essay questions (15-20 marks). "
                  "Use command words such as Explain, Discuss, Compare, Critically evaluate, Apply. An essay question "
                  "may have no parts; a short-answer question may have parts (a), (b).",
    },
    "waec": {
        "mcq": "WAEC SSCE objective paper in the style of real past questions: 4 options (A-D). One correct answer. "
               "Syllabus-style stems a secondary-school student would meet, including 'Which of the following...', "
               "short scenarios, and simple calculations or diagrams described in words where the notes allow.",
        "theory": "WAEC SSCE theory paper: structured questions with numbered parts (a), (b), (c) and where useful "
                  "sub-parts (i), (ii), each part with its marks, total 10-15 marks per question. Use WAEC command "
                  "words: State, Define, List two, Explain, Describe, Calculate, Give two reasons.",
    },
    "jamb": {
        "mcq": "JAMB UTME objective style, like real past questions: 4 options (A-D), single best answer, short direct "
               "stems, quick to read, testing key facts and their application. Distractors close enough that guessing is hard.",
        "theory": "JAMB itself is all objective, so make short-answer questions (2-6 marks) that check the same "
                  "facts in the student's own words, with parts (a), (b) where useful.",
    },
    "general": {
        "mcq": "Clear multiple-choice questions: 4 options, one correct answer.",
        "theory": "Clear written-answer questions with marks: short answers (3-6 marks) and some longer ones "
                  "(8-12 marks), with parts where helpful.",
    },
}

DIFF_GUIDE = {
    "easy": "easy (direct recall of stated facts and definitions)",
    "medium": "medium (understanding: explain, link ideas, apply to a simple case)",
    "hard": "hard (analysis and application: multi-step reasoning, comparing ideas, unfamiliar cases, still answerable from the pages)",
    "mixed": "a mix of easy, medium and hard (roughly 30/45/25)",
}

MCQ_SHAPE = {"questions": [{
    "type": "mcq", "question": "<question stem>", "options": ["<A>", "<B>", "<C>", "<D>"], "answer": 0,
    "explanation": "<2-4 sentences that TEACH: why the answer is right (the idea behind it, citing the page as 'p. 3'), "
                   "then why the most tempting wrong option is wrong>",
    "misconception": "<the common misunderstanding the most tempting distractor is built on, one sentence>",
    "skill": "recall|understanding|application", "pages": [3],
    "topic": "<short topic name, 1-4 words>", "difficulty": "easy|medium|hard"}]}

THEORY_SHAPE = {"questions": [{
    "type": "theory", "question": "<question stem or context>", "format": "structured|short|essay",
    "parts": [{"label": "a", "text": "<part question>", "marks": 4}, {"label": "b", "text": "<part question>", "marks": 6}],
    "marks": 10, "model_answer": "<a complete model answer a top student would write, by part>",
    "marking_points": [{"point": "<one creditable point the examiner looks for>", "marks": 2}],
    "pages": [3, 4], "topic": "<short topic name, 1-4 words>", "difficulty": "easy|medium|hard"}]}


def generation_system(kind: str, req: dict) -> str:
    guide = STYLE_GUIDE[req["style"]][kind]
    shape = MCQ_SHAPE if kind == "mcq" else THEORY_SHAPE
    extra = ("'answer' is the 0-based index of the correct option. Exactly one option is correct; no 'all of the "
             "above' or 'none of the above'. Options are short, similar in length and grammatically parallel.\n"
             "- Test UNDERSTANDING, not just recall: at least 60% of questions must be 'understanding' or 'application' "
             "(why/how, cause and effect, apply the idea to a new example or scenario, interpret a statement). Use "
             "'recall' only for key definitions.\n"
             "- Every distractor must be plausible: build each one from a real misconception, a confusable term from "
             "the notes, or a half-right idea. Never use joke or obviously wrong options." if kind == "mcq" else
             "Marking points must add up to the question's total marks, and so must the parts' marks. Each marking "
             "point is something specific a marker can tick. The model answer covers every marking point.")
    return f"""{GEN_MARKER}
You are an experienced examiner writing practice questions from a student's own notes.
Style: {STYLE_NAMES[req['style']]}. {guide}
Difficulty: {DIFF_GUIDE[req['difficulty']]}.
Rules (mandatory):
- Every question must be answerable from the PAGES below only. Do not test facts that are not in them.
- Never copy a sentence from the notes as the stem; rephrase so the student must understand, not pattern-match.
- 'pages' lists the page number(s) from the === Page N === markers where the answer is found.
- Spread questions across the pages; no two questions test the same fact.
- {extra}
- Write in clear English. Markdown is allowed in strings; math as $...$.
- Output strict JSON only: one object, double-quoted keys and strings, no commentary, no code fences.

Return JSON exactly in this shape:
{json.dumps(shape, indent=1)}
"""


def generation_user(kind: str, n: int, req: dict, pages: dict[int, str], avoid: list[str]) -> str:
    focus = f"\nFocus topic: {req['topic']} (prefer questions on this; use other material only if needed)." if req["topic"] else ""
    avoid_block = ("\nAlready asked (do not repeat these):\n" + "\n".join(f"- {a[:160]}" for a in avoid[-30:])) if avoid else ""
    if req.get("weak_topics") or req.get("missed"):
        missed = "\n".join(f"- {m[:200]}" for m in req.get("missed", [])[:12])
        focus += ("\nADAPTIVE PRACTICE: the student is weak on: " + (", ".join(req.get("weak_topics", [])) or "the concepts below") +
                  ". Write NEW questions on these exact concepts from different angles (do not reuse the stems below). "
                  "Start with one or two easier questions to rebuild confidence, then build up to application. "
                  "Target the misconceptions their wrong answers suggest." + (f"\nQuestions they got wrong:\n{missed}" if missed else ""))
    meta = json.dumps({"kind": kind, "count": n, "pages": sorted(pages)})
    label = "multiple-choice (objective)" if kind == "mcq" else "theory (written-answer)"
    return (f"EXAM_REQUEST: {meta}\nWrite exactly {n} {label} questions.{focus}{avoid_block}\n\n"
            f"PAGES:\n\n{context_block(pages)}")


# ---------- normalisers ----------

def _questions_list(data) -> list:
    if isinstance(data, dict):
        for key in ("questions", "items", "exam"):
            if isinstance(data.get(key), list):
                return data[key]
        if "question" in data:
            return [data]
    if isinstance(data, list):
        return data
    raise ValueError("expected a list of questions")


def _answer_index(q: dict, options: list[str]) -> int:
    raw = q.get("answer", q.get("correct", q.get("correct_answer")))
    if isinstance(raw, bool):
        return -1
    if isinstance(raw, (int, float)):
        return int(raw)
    text = _s(raw)
    if re.fullmatch(r"[A-Fa-f][).:]?", text):
        return "abcdef".index(text[0].lower())
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.match(r"^\(?([A-Fa-f])[).:]\s*(.*)$", text)
    if m and m.group(2):
        text = m.group(2)
    norm = [_norm_text(o) for o in options]
    return norm.index(_norm_text(text)) if _norm_text(text) in norm else -1


_LETTER_PREFIX = re.compile(r"^\s*\(?[A-Ea-e][).:]\s+")


def normalize_mcq(q: dict, lo: int, hi: int, pages: dict[int, str]) -> dict | None:
    question = _s(q.get("question"), 1200)
    options = [_LETTER_PREFIX.sub("", o) for o in _str_list(q.get("options"), 6, 400)]
    if not question or len(options) < 3:
        return None
    if len({_norm_text(o) for o in options}) != len(options):
        return None
    answer = _answer_index(q, options)
    if not 0 <= answer < len(options):
        return None
    if len(options) > 5:  # keep the correct answer, drop extra distractors
        correct = options[answer]
        options = [o for o in options if o != correct][:4] + [correct]
        answer = len(options) - 1
    correct = options[answer]
    rng = random.Random(hashlib.md5(question.encode()).hexdigest())  # models favour "A"; unbias deterministically
    rng.shuffle(options)
    explanation = _s(q.get("explanation"), 1500)
    refs = parse_pages(q.get("pages", q.get("page")), lo, hi)
    if not refs:
        guess = best_page(f"{question} {correct} {explanation}", pages)
        refs = [guess] if guess else []
    skill = q.get("skill") if q.get("skill") in ("recall", "understanding", "application") else "understanding"
    return {"type": "mcq", "question": question, "options": options, "answer": options.index(correct),
            "explanation": explanation, "misconception": _s(q.get("misconception"), 400), "skill": skill,
            "pages": refs, "topic": _s(q.get("topic"), 60) or "General",
            "difficulty": q.get("difficulty") if q.get("difficulty") in ("easy", "medium", "hard") else "medium"}


def normalize_theory(q: dict, lo: int, hi: int, pages: dict[int, str]) -> dict | None:
    question = _s(q.get("question"), 2000)
    parts = []
    for i, p in enumerate(q.get("parts") or []):
        if isinstance(p, dict) and _s(p.get("text")):
            label = re.sub(r"[()\s.]", "", _s(p.get("label"), 8)) or "abcdefghij"[min(i, 9)]
            parts.append({"label": label, "text": _s(p.get("text"), 1200), "marks": max(1, min(30, _int(p.get("marks"), 2)))})
        elif isinstance(p, str) and p.strip():
            parts.append({"label": "abcdefghij"[min(i, 9)], "text": p.strip()[:1200], "marks": 2})
    if not question and parts:
        question = "Answer all parts."
    if not question:
        return None
    points = []
    for p in q.get("marking_points") or q.get("marking_scheme") or []:
        if isinstance(p, dict) and _s(p.get("point") or p.get("text")):
            points.append({"point": _s(p.get("point") or p.get("text"), 500), "marks": max(1, min(20, _int(p.get("marks"), 1)))})
        elif isinstance(p, str) and p.strip():
            points.append({"point": p.strip()[:500], "marks": 1})
    points = points[:20]
    if not points:
        return None
    total = sum(p["marks"] for p in points)
    if parts and sum(p["marks"] for p in parts) != total:
        # The scheme decides the score; rescale part marks so the paper the student sees adds up.
        scale = total / max(1, sum(p["marks"] for p in parts))
        for p in parts:
            p["marks"] = max(1, round(p["marks"] * scale))
        parts[-1]["marks"] = max(1, parts[-1]["marks"] + total - sum(p["marks"] for p in parts))
    model = _s(q.get("model_answer") or q.get("answer"), 6000) or "\n".join(f"- {p['point']}" for p in points)
    refs = parse_pages(q.get("pages", q.get("page")), lo, hi)
    if not refs:
        guess = best_page(f"{question} {model}", pages)
        refs = [guess] if guess else []
    fmt = q.get("format") if q.get("format") in ("structured", "short", "essay") else ("structured" if parts else "short")
    return {"type": "theory", "question": question, "format": fmt, "parts": parts[:8], "marks": total,
            "model_answer": model, "marking_points": points, "pages": refs, "topic": _s(q.get("topic"), 60) or "General",
            "difficulty": q.get("difficulty") if q.get("difficulty") in ("easy", "medium", "hard") else "medium"}


def normalize_questions(data, kind: str, lo: int, hi: int, pages: dict[int, str]) -> list[dict]:
    fn = normalize_mcq if kind == "mcq" else normalize_theory
    out = []
    for q in _questions_list(data):
        if isinstance(q, dict):
            item = fn(q, lo, hi, pages)
            if item:
                out.append(item)
    if not out:
        raise ValueError(f"no valid {kind} questions")
    return out


def complete_json(provider, system: str, user: str, normalize: Callable[[object], object], max_tokens: int):
    """Ask for JSON, parse, validate; on failure retry once with a firmer instruction, then give up politely."""
    prompt, last = user, None
    for _ in range(2):
        raw = provider.complete(system, prompt, json_mode=True, max_tokens=max_tokens)
        try:
            return normalize(parse_json(raw))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            last = exc
            prompt = (user + "\n\nIMPORTANT: your previous reply was not valid JSON in the required shape. "
                      "Reply with ONLY the JSON object, nothing else.")
    raise ExamOutputError(str(last))


# ---------- generation ----------

def _batches(n: int, size: int) -> list[int]:
    out = []
    while n > 0:
        out.append(min(size, n))
        n -= out[-1]
    return out


def generate_exam(provider, store: VectorStore, doc_id: str, opts: dict) -> dict:
    info = store.info(doc_id) or {}
    all_pages = page_texts(store, doc_id)
    doc_pages = max([info.get("pages") or 0, *all_pages.keys(), 1])
    req = resolve_request(opts, doc_pages)
    chosen = select_pages(all_pages, req["page_from"], req["page_to"])
    if not chosen:
        raise ExamInputError(f"Pages {req['page_from']}-{req['page_to']} have no readable text. Try another range.")
    lo, hi = req["page_from"], req["page_to"]

    jobs = []  # (kind, n, pages)
    for kind, n, size in (("mcq", req["n_mcq"], MCQ_BATCH), ("theory", req["n_theory"], THEORY_BATCH)):
        sizes = _batches(n, size)
        for count, group in zip(sizes, split_pages(chosen, len(sizes))):
            jobs.append((kind, count, fit_context(group, req["topic"])))

    def run(job, avoid=()):
        kind, n, group = job
        max_tokens = min(8192, 600 + n * (350 if kind == "mcq" else 900))
        qs = complete_json(provider, generation_system(kind, req), generation_user(kind, n, req, group, list(avoid)),
                           lambda d: normalize_questions(d, kind, lo, hi, chosen), max_tokens)
        return kind, n, qs

    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        results = list(pool.map(run, jobs))

    by_kind: dict[str, list[dict]] = {"mcq": [], "theory": []}
    seen: set[str] = set()
    for kind, _, qs in results:
        for q in qs:
            key = _norm_text(q["question"])[:120]
            if key in seen:
                continue
            seen.add(key)
            by_kind[kind].append(q)
    # Top up once if the model returned fewer valid questions than asked (dropped invalid or duplicate ones).
    for kind, want in (("mcq", req["n_mcq"]), ("theory", req["n_theory"])):
        missing = want - len(by_kind[kind])
        if missing > 0 and by_kind[kind]:
            try:
                _, _, extra = run((kind, missing, fit_context(chosen, req["topic"])), [q["question"] for q in by_kind[kind]])
                for q in extra:
                    key = _norm_text(q["question"])[:120]
                    if key not in seen:
                        seen.add(key)
                        by_kind[kind].append(q)
            except ExamOutputError:
                pass
        by_kind[kind] = by_kind[kind][:want]
    questions = by_kind["mcq"] + by_kind["theory"]
    if not questions:
        raise ExamOutputError("no questions")
    for i, q in enumerate(questions, 1):
        q["id"] = f"q{i}"
    return {"doc_id": doc_id, "filename": info.get("filename", ""), "settings": req, "questions": questions,
            "total_marks": sum(1 if q["type"] == "mcq" else q["marks"] for q in questions)}


# ---------- marking ----------

def mark_mcq(question: dict, choice) -> dict:
    options = question.get("options") or []
    answer = _int(question.get("answer"), -1)
    picked = None if choice is None or isinstance(choice, bool) else _int(choice, -1)
    ok = picked is not None and 0 <= picked < len(options) and picked == answer
    return {"type": "mcq", "correct": ok, "choice": picked, "answer": answer, "score": 1 if ok else 0, "max": 1,
            "explanation": _s(question.get("explanation"), 1500), "pages": question.get("pages") or []}


MARK_SHAPE = {"results": [{
    "id": "q1", "awarded": [{"point": 0, "marks": 2, "comment": "<why this mark>"}],
    "good": ["<what the answer did well>"], "missing": ["<a marking point the answer missed or got wrong>"],
    "short_answer": "<a SHORT model answer for this question, 2-4 sentences or bullet points, that would earn full marks>",
    "improve": ["<one concrete step to gain the missing marks next time, e.g. 'State the definition first, then give an example'>"],
    "feedback": "<one or two sentences of encouraging, specific advice>"}]}


def marking_system(style: str) -> str:
    return f"""{MARK_MARKER}
You are a fair, experienced {STYLE_NAMES.get(style, 'General')} examiner marking a student's practice answers.
For each answer, go through the marking points one by one. 'point' is the 0-based index of the marking point.
Award marks for a point when the answer clearly makes it, in any wording; partial marks are allowed for a point
that is only partly made. Do not award marks for points the answer does not make, however long it is. Ignore
spelling and grammar unless they change the meaning. Never give more than a point's marks.
'good' lists what the student did well (empty list if nothing). 'missing' lists what they missed or got wrong,
phrased so they can learn it. 'short_answer' is a brief full-mark answer in plain words; 'improve' gives 1-3
concrete, actionable steps (exam technique or content) to score higher. Keep feedback kind and specific.
Output strict JSON only, no commentary, no code fences.

Return JSON exactly in this shape:
{json.dumps(MARK_SHAPE, indent=1)}
"""


def marking_user(items: list[dict]) -> str:
    blocks = []
    for it in items:
        q = it["question"]
        parts = "\n".join(f"({p['label']}) {p['text']} [{p['marks']} marks]" for p in q.get("parts") or [])
        points = "\n".join(f"{i}. {p['point']} [{p['marks']} marks]" for i, p in enumerate(q["marking_points"]))
        blocks.append(f"### Answer id: {it['id']}\nQuestion ({q['marks']} marks): {q['question']}\n{parts}\n"
                      f"Marking points:\n{points}\nModel answer: {q.get('model_answer', '')[:2500]}\n"
                      f"Student's answer:\n<<<\n{it['answer'][:MAX_ANSWER_CHARS]}\n>>>")
    return "MARK_REQUEST: " + json.dumps([it["id"] for it in items]) + "\n\n" + "\n\n".join(blocks)


def clean_theory_question(q: dict) -> dict:
    """Validate a theory question sent back by the browser before using it in a marking prompt."""
    if not isinstance(q, dict):
        raise ExamInputError("Missing question")
    item = normalize_theory(q, 1, 100000, {})
    if not item:
        raise ExamInputError("This question has no marking scheme, so it can't be marked.")
    item["pages"] = parse_pages(q.get("pages"), 1, 100000)
    return item


def normalize_marks(data, items: list[dict]) -> dict[str, dict]:
    rows = data.get("results") if isinstance(data, dict) else data
    if isinstance(data, dict) and rows is None and "awarded" in data:
        rows = [dict(data, id=data.get("id") or items[0]["id"])]
    if not isinstance(rows, list):
        raise ValueError("expected results list")
    ids = [it["id"] for it in items]
    by_id: dict[str, dict] = {}
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            continue
        rid = _s(r.get("id"), 40)
        if rid not in ids:
            rid = ids[i] if i < len(ids) and ids[i] not in by_id else ""
        if not rid:
            continue
        q = next(it["question"] for it in items if it["id"] == rid)
        pts = q["marking_points"]
        awarded = [0.0] * len(pts)
        comments = [""] * len(pts)
        for a in r.get("awarded") or []:
            if not isinstance(a, dict):
                continue
            k = _int(a.get("point", a.get("index")), -1)
            if 0 <= k < len(pts):
                awarded[k] = max(0.0, min(float(pts[k]["marks"]), _num(a.get("marks"))))
                comments[k] = _s(a.get("comment"), 300)
        score = round(sum(awarded) * 2) / 2  # half marks at most
        if not r.get("awarded") and "score" in r:  # model skipped the per-point breakdown
            score = max(0.0, min(float(q["marks"]), round(_num(r.get("score")) * 2) / 2))
        by_id[rid] = {"type": "theory", "score": score, "max": q["marks"],
                      "points": [{"point": p["point"], "marks": p["marks"], "awarded": awarded[k], "comment": comments[k]}
                                 for k, p in enumerate(pts)],
                      "good": _str_list(r.get("good"), 6), "missing": _str_list(r.get("missing"), 8),
                      "feedback": _s(r.get("feedback"), 800), "short_answer": _s(r.get("short_answer"), 1200),
                      "improve": _str_list(r.get("improve"), 4, 300),
                      "model_answer": q["model_answer"], "pages": q.get("pages") or []}
    if not by_id:
        raise ValueError("no marks")
    return by_id


def mark_theory(provider, style: str, items: list[dict]) -> dict[str, dict]:
    """items: [{id, question (validated theory dict), answer}]. Blank answers score 0 without an AI call."""
    out: dict[str, dict] = {}
    todo = []
    for it in items:
        if len(it["answer"].strip()) < 2:
            q = it["question"]
            out[it["id"]] = {"type": "theory", "score": 0, "max": q["marks"],
                             "points": [{"point": p["point"], "marks": p["marks"], "awarded": 0, "comment": ""} for p in q["marking_points"]],
                             "good": [], "missing": [p["point"] for p in q["marking_points"]],
                             "feedback": "No answer was written, so no marks could be given. Read the model answer, then try it again.",
                             "model_answer": q["model_answer"], "pages": q.get("pages") or []}
        else:
            todo.append(it)
    if todo:
        marked = complete_json(provider, marking_system(style), marking_user(todo), lambda d: normalize_marks(d, todo),
                               min(8192, 500 + 450 * len(todo)))
        for it in todo:
            if it["id"] not in marked:
                raise ExamOutputError(f"answer {it['id']} was not marked")
        out.update(marked)
    return out


# ---------- offline fake (tests and demos without an API key) ----------

def fake_reply(system: str, user: str) -> str:
    """Deterministic, valid replies for FakeProvider so the whole flow works offline."""
    if MARK_MARKER in system:
        ids = json.loads(re.search(r"MARK_REQUEST: (\[.*?\])", user).group(1))
        results = []
        for rid in ids:
            block = user.split(f"### Answer id: {rid}", 1)[1].split("### Answer id:", 1)[0]
            pts = re.findall(r"^(\d+)\. (.*) \[(\d+) marks\]$", block, re.M)
            answer = _words(block.split("<<<", 1)[1].split(">>>", 1)[0])
            awarded = [{"point": int(i), "marks": int(m) if len(answer & _words(p)) >= 2 else 0, "comment": "offline demo"}
                       for i, p, m in pts]
            results.append({"id": rid, "awarded": awarded, "good": ["(offline demo) Covers some key points."],
                            "missing": [p for (i, p, m), a in zip(pts, awarded) if not a["marks"]],
                            "short_answer": "(offline demo) " + (pts[0][1] if pts else "See the model answer."),
                            "improve": ["(offline demo) Cover every marking point in order."],
                            "feedback": "(offline demo) Compare your answer with the model answer."})
        return json.dumps({"results": results})
    meta = json.loads(re.search(r"EXAM_REQUEST: (\{.*?\})\n", user).group(1))
    pages = {int(p): t.strip() for p, t in re.findall(r"=== Page (\d+) ===\n(.*?)(?=\n\n=== Page |\Z)", user, re.S)}
    sentences = [(p, s.strip()) for p, t in pages.items() for s in re.split(r"(?<=[.!?])\s+|\n", t) if len(s.strip()) > 12]
    sentences = sentences or [(next(iter(pages), 1), "the document")]
    qs = []
    for i in range(meta["count"]):
        p, s = sentences[i % len(sentences)]
        tag = f"#{i + 1}"
        if meta["kind"] == "mcq":
            qs.append({"type": "mcq", "question": f"(offline demo {tag}) Which statement appears on page {p}?",
                       "options": [s[:160], f"The opposite of: {s[:60]} ({tag}a)", f"Not mentioned ({tag}b)", f"None of these ({tag}c)"],
                       "answer": 0, "explanation": f"Page {p} says: {s[:200]}", "pages": [p], "skill": "recall",
                       "misconception": "(offline demo) Confusing what the notes say with its opposite.",
                       "topic": f"Page {p}", "difficulty": "easy"})
        else:
            qs.append({"type": "theory", "question": f"(offline demo {tag}) Explain what page {p} says.", "format": "structured",
                       "parts": [{"label": "a", "text": "State the main point.", "marks": 2}, {"label": "b", "text": "Explain it.", "marks": 2}],
                       "marks": 4, "model_answer": s, "marking_points": [{"point": s[:200], "marks": 2}, {"point": f"Explains {s[:80]}", "marks": 2}],
                       "pages": [p], "topic": f"Page {p}", "difficulty": "medium"})
    return json.dumps({"questions": qs})
