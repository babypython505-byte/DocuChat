// DocuChat Exam Prep. Vanilla ES module, shares the PDF library (IndexedDB "docuchat") with the chat app.
// Flow: setup -> generate (POST /api/exam/generate) -> practice (timer, navigator, instant or end-of-exam marking)
// -> results (score, time, topic/page breakdown, weak areas, retry). MCQs are marked in the browser (the answer key
// comes with the set); theory answers go to POST /api/exam/mark. Progress history and the in-progress attempt live in
// localStorage so a refresh or a closed tab never loses work.

const PDFJS = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build";
const LS_CURRENT = "dc-exam-current";
const LS_HISTORY = "dc-exam-history";
const LS_PREFS = "dc-exam-prefs";
const MAX_HISTORY = 25;
const MARK_BATCH = 10;          // server marks at most 10 written answers per request
const SYNC_STALE_MS = 3 * 60e3;
const WEAK_PCT = 60;

/* ---------------- utilities ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c.nodeType ? c : document.createTextNode(String(c)));
  return node;
}
function icon(name, cls = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (cls) svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
const clamp = (n, a, b) => Math.max(a, Math.min(b, n));
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const LETTERS = "ABCDEF";
const STYLE_NAMES = { university: "University", waec: "WAEC", jamb: "JAMB / UTME", general: "General" };
const QTYPE_NAMES = { mcq: "Objective", theory: "Theory", mixed: "Mixed" };
function fmtTime(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtDuration(ms) {
  const m = Math.round(ms / 60000);
  if (ms < 60000) return `${Math.max(1, Math.round(ms / 1000))} sec`;
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`;
}
function fmtDate(ts) {
  const d = new Date(ts);
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + " · " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
const pct = (score, max) => (max ? Math.round((score / max) * 100) : 0);
function pagesLabel(pages) {
  if (!pages?.length) return "";
  return pages.length === 1 ? `page ${pages[0]}` : `pages ${pages.join(", ")}`;
}
function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function mdToSafeHtml(md, inline = false) {
  md = String(md || "");
  const math = [];
  const ph = (tex, display) => { math.push({ tex, display }); return `MATHPH${math.length - 1}XEND`; };
  const parts = md.split(/(```[\s\S]*?```|`[^`\n]+`)/g);
  md = parts.map((p, i) => (i % 2 ? p : p
    .replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => ph(t, true))
    .replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => ph(t, true))
    .replace(/\\\(([\s\S]+?)\\\)/g, (_, t) => ph(t, false))
    .replace(/(^|[^\\$\w])\$(?![\s\d])([^$\n]+?)(?<!\s)\$(?![\w$])/g, (_, pre, t) => pre + ph(t, false))
    .replace(/(^|[^\\$\w])\$(?=\d)([^$\n]*?[=^_\\][^$\n]*?)(?<!\s)\$(?![\w$])/g, (_, pre, t) => pre + ph(t, false))
  )).join("");
  let html;
  if (window.marked && window.DOMPurify) html = inline ? window.marked.parseInline(md, { gfm: true }) : window.marked.parse(md, { gfm: true, breaks: true });
  else html = escapeHtml(md).replace(/\n/g, "<br>");
  html = html.replace(/MATHPH(\d+)XEND/g, (_, i) => {
    const { tex, display } = math[+i];
    if (window.katex) {
      try { return window.katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: "ignore", trust: false, output: "htmlAndMathml" }); }
      catch { /* fall through */ }
    }
    return escapeHtml(display ? `$$${tex}$$` : `$${tex}$`);
  });
  if (window.DOMPurify) return window.DOMPurify.sanitize(html, { FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe"] });
  return html.replace(/<(?!\/?(br|span|math|annotation|semantics|mrow|mi|mo|mn|msup|msub|mfrac|svg|path)\b)[^>]*>/gi, "");
}
const md = (text, inline = false, tag = "div", cls = "md") => el(tag, { class: cls, html: mdToSafeHtml(text, inline) });
const lsGet = (k, fallback) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : fallback; } catch { return fallback; } };
function lsSet(k, v) {
  try { localStorage.setItem(k, JSON.stringify(v)); return true; }
  catch (e) { console.warn("localStorage:", e?.message || e); return false; }
}

/* ---------------- IndexedDB (same database as the chat app) ---------------- */
const idb = (() => {
  let dbp;
  const open = () => dbp ||= new Promise((resolve, reject) => {
    if (!("indexedDB" in window)) return reject(new Error("IndexedDB unavailable"));
    const req = indexedDB.open("docuchat", 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("docs")) db.createObjectStore("docs", { keyPath: "id" });
      if (!db.objectStoreNames.contains("convs")) db.createObjectStore("convs", { keyPath: "id" });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  const run = async (store, mode, fn) => {
    const db = await open();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(store, mode);
      const r = fn(tx.objectStore(store));
      tx.oncomplete = () => resolve(r?.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () => reject(tx.error || new Error("IndexedDB transaction aborted"));
    });
  };
  return {
    get: (s, k) => run(s, "readonly", (st) => st.get(k)),
    all: (s) => run(s, "readonly", (st) => st.getAll()),
    put: (s, v) => run(s, "readwrite", (st) => st.put(v)),
  };
})();
const safeIdb = async (fn, fallback) => { try { return await fn(); } catch (e) { console.warn("storage:", e?.message || e); return fallback; } };

/* ---------------- state ---------------- */
const state = {
  config: null,
  usage: null,
  docs: [],            // {id, filename, pages, status: ready|stale|serverOnly}
  uploading: false,
  attempt: null,       // the exam being taken or just finished
  view: "setup",
  timer: null,
  lastTick: 0,
  lastOk: 0,
  minutesTouched: false,
  pdf: { lib: null, doc: null, docId: null },
};
const docById = (id) => state.docs.find((d) => d.id === id);

/* ---------------- API ---------------- */
class ApiError extends Error { constructor(status, detail, body) { super(detail); this.status = status; this.body = body; } }
async function toApiError(res) {
  let body = {};
  try { body = await res.json(); } catch { /* non-JSON */ }
  let detail = body.detail;
  if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
  return new ApiError(res.status, detail || res.statusText || "Request failed", body);
}
async function api(path, { method = "GET", body, retry = true } = {}) {
  let res;
  try {
    res = await fetch(path, { method, credentials: "same-origin", headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  } catch {
    throw new ApiError(0, "Can't reach DocuChat. Check your connection; the server may be waking up (about 30 seconds).");
  }
  if (res.status === 409 && retry) {
    const err = await toApiError(res);
    if (err.body?.code === "missing_documents") {   // server restarted: re-sync from this browser, then retry once
      for (const id of err.body.missing || []) await restoreDoc(id);
      return api(path, { method, body, retry: false });
    }
    throw err;
  }
  if (!res.ok) throw await toApiError(res);
  state.lastOk = Date.now();
  return res.json();
}
async function ensureSynced() { if (Date.now() - state.lastOk > SYNC_STALE_MS) await syncDocs({ silent: true }); }
function handleError(err, context = "") {
  const s = err?.status;
  const msg = err?.message || "Something went wrong.";
  if (s === 429) toast(msg, "error", { ms: 9000, link: { href: "mailto:babypython505@gmail.com?subject=DocuChat%20Pro", text: "Join Pro waitlist" } });
  else if (s === 502 || s === 503) toast(msg.replace(/\s*\(.*\)$/, ""), "error", { ms: 8000 });
  else if ([400, 402, 413, 422].includes(s)) toast(`${context ? context + ": " : ""}${msg}`, "warn", { ms: 7000 });
  else toast(msg, "error", { ms: 7000 });
}
function setUsage(u) { if (u) { state.usage = u; renderUsage(); } }

/* ---------------- toasts ---------------- */
function toast(text, kind = "info", { ms = 4500, link } = {}) {
  const t = el("div", { class: `toast ${kind}`, role: kind === "error" ? "alert" : "status" },
    el("span", { class: "dot" }),
    el("div", {}, text, link ? [" ", el("a", { href: link.href, text: link.text })] : null),
    el("button", { class: "icon-btn", "aria-label": "Dismiss", onclick: () => close() }, icon("x")));
  const close = () => { t.classList.add("out"); setTimeout(() => t.remove(), 200); };
  $("#toasts").append(t);
  while ($("#toasts").children.length > 4) $("#toasts").firstChild.remove();
  setTimeout(close, ms);
}

/* ---------------- library: load, upload, re-sync ---------------- */
async function loadDocs() {
  const recs = await safeIdb(() => idb.all("docs"), []);
  state.docs = (recs || []).sort((a, b) => a.addedAt - b.addedAt)
    .map((r) => ({ id: r.id, filename: r.filename, pages: r.pages, status: r.index ? "ready" : "stale" }));
}
async function restoreDoc(id) {
  const d = docById(id);
  const rec = await safeIdb(() => idb.get("docs", id), null);
  if (!rec?.index) { if (d) d.status = "stale"; return false; }
  try {
    const r = await api("/api/documents/restore", { method: "POST", retry: false, body: {
      id: rec.id, filename: rec.filename, pages: rec.pages, meta: rec.meta || {},
      dim: rec.index.dim, dtype: rec.index.dtype || "float16", chunks: rec.index.chunks, vectors: rec.index.vectors } });
    if (d) d.status = "ready";
    setUsage(r.usage);
    return true;
  } catch (e) {
    if (e.status === 422) { if (d) d.status = "stale"; toast(`${rec.filename} needs to be re-uploaded (the AI model changed).`, "warn", { ms: 8000 }); }
    else if (e.status === 402) toast(e.message, "warn");
    else throw e;
    return false;
  }
}
async function syncDocs({ silent = false } = {}) {
  let server;
  try { server = await api("/api/documents"); }
  catch (e) { if (!silent) handleError(e); return; }
  const have = new Set(server.map((d) => d.id));
  for (const d of state.docs) {
    if (!have.has(d.id) && d.status !== "stale") { try { await restoreDoc(d.id); } catch (e) { if (!silent) handleError(e); } }
  }
  for (const s of server) if (!docById(s.id)) state.docs.push({ id: s.id, filename: s.filename, pages: s.pages, status: "serverOnly" });
  renderDocSelect();
}
function uploadFile(file) {
  const maxMb = state.config?.max_upload_mb ?? 20;
  const limit = state.config?.docs_limit ?? 3;
  if (!(/\.pdf$/i.test(file.name) || file.type === "application/pdf")) return toast("Only PDF files can be added.", "warn");
  if (file.size > maxMb * 1024 * 1024) return toast(`${file.name} is over ${maxMb} MB.`, "warn");
  if (state.docs.filter((d) => d.status !== "stale").length >= limit) {
    return toast(`The free plan holds ${limit} PDFs. Remove one in Chat with PDFs to add another.`, "warn", { ms: 7000 });
  }
  state.uploading = true;
  const label = $("#upload-label span");
  label.textContent = "Uploading… 0%";
  const done = () => { state.uploading = false; label.textContent = "Upload PDF"; $("#file-input").value = ""; };
  const xhr = new XMLHttpRequest();
  const fd = new FormData(); fd.append("file", file, file.name);
  xhr.open("POST", "/api/documents");
  xhr.responseType = "json";
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) label.textContent = e.loaded >= e.total ? "Reading pages…" : `Uploading… ${Math.round((e.loaded / e.total) * 100)}%`; };
  xhr.onerror = () => { done(); toast("Upload failed. Check your connection and try again.", "error"); };
  xhr.onload = async () => {
    const body = xhr.response || {};
    if (xhr.status !== 201) {
      done();
      let detail = body.detail; if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
      return handleError(new ApiError(xhr.status, detail || `Upload failed (${xhr.status})`, body), file.name);
    }
    const rec = { id: body.id, filename: body.filename, pages: body.pages, chunks: body.chunks, meta: body.meta || {}, size: file.size, addedAt: Date.now(), blob: file, index: body.index };
    await safeIdb(() => idb.put("docs", rec));
    const sel = JSON.parse(localStorage.getItem("dc-selected") || "null");
    if (Array.isArray(sel)) { sel.push(rec.id); localStorage.setItem("dc-selected", JSON.stringify(sel)); }  // chat app sees it too
    state.docs = state.docs.filter((d) => d.id !== rec.id);
    state.docs.push({ id: rec.id, filename: rec.filename, pages: rec.pages, status: "ready" });
    setUsage(body.usage); state.lastOk = Date.now();
    done();
    renderDocSelect(rec.id);
    toast(`${rec.filename} is ready · ${plural(rec.pages, "page")}`, "ok");
  };
  xhr.send(fd);
}

/* ---------------- page text + page viewer ---------------- */
function mergeOverlap(a, b) {
  if (!a) return b;
  for (let k = Math.min(a.length, b.length, 600); k > 11; k--) if (a.endsWith(b.slice(0, k))) return a + b.slice(k);
  return `${a}\n${b}`;
}
async function pageText(docId, page) {
  const rec = await safeIdb(() => idb.get("docs", docId), null);
  const chunks = rec?.index?.chunks || [];
  return chunks.filter((c) => c.page === page).reduce((acc, c) => mergeOverlap(acc, c.text), "");
}
async function pdfLib() {
  if (state.pdf.lib) return state.pdf.lib;
  const lib = await import(`${PDFJS}/pdf.min.mjs`);
  lib.GlobalWorkerOptions.workerSrc = `${PDFJS}/pdf.worker.min.mjs`;
  return (state.pdf.lib = lib);
}
async function openPage(docId, page) {
  const dlg = $("#dlg-page");
  const box = $("#page-text");
  const name = docById(docId)?.filename || state.attempt?.filename || "your notes";
  $("#page-title").textContent = `Page ${page} · ${name}`;
  box.replaceChildren(el("span", { class: "thinking" }, el("i"), el("i"), el("i")));
  if (!dlg.open) dlg.showModal();
  const rec = await safeIdb(() => idb.get("docs", docId), null);
  if (rec?.blob) {
    try {
      const lib = await pdfLib();
      if (state.pdf.docId !== docId) {
        if (state.pdf.doc) state.pdf.doc.destroy();
        state.pdf.doc = await lib.getDocument({ data: new Uint8Array(await rec.blob.arrayBuffer()), isEvalSupported: false }).promise;
        state.pdf.docId = docId;
      }
      const p = await state.pdf.doc.getPage(clamp(page, 1, state.pdf.doc.numPages));
      const width = Math.min(box.clientWidth || 640, 760);
      const base = p.getViewport({ scale: 1 });
      const scale = width / base.width;
      const vp = p.getViewport({ scale: scale * (window.devicePixelRatio || 1) });
      const canvas = el("canvas", { class: "page-canvas", width: Math.floor(vp.width), height: Math.floor(vp.height) });
      canvas.style.width = `${Math.floor(base.width * scale)}px`;
      await p.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
      box.replaceChildren(canvas);
      return;
    } catch (e) { console.warn("pdf.js:", e); }
  }
  const text = await pageText(docId, page);
  box.replaceChildren(text ? el("pre", { class: "page-plain", text }) : el("p", { class: "muted", text: "This page isn't stored in this browser. Open your PDF and turn to this page." }));
}
function pageLinks(pages, docId) {
  if (!pages?.length) return null;
  return el("span", { class: "see-pages" }, "See ", pages.map((p, i) => [i ? ", " : "", el("button", { class: "linkish", type: "button", onclick: () => openPage(docId, p) }, `page ${p}`)]));
}

/* ---------------- setup form ---------------- */
function prefs() { return lsGet(LS_PREFS, {}); }
function savePrefs() {
  const f = readForm();
  lsSet(LS_PREFS, { qtype: f.qtype, count: f.count, theory_count: f.theory_count, style: f.style, difficulty: f.difficulty, timing: f.timing, instant: f.instant, docId: f.doc_id });
}
function radio(name) { return $(`input[name=${name}]:checked`)?.value; }
function setRadio(name, value) { const r = $(`input[name=${name}][value="${value}"]`); if (r) r.checked = true; }
function readForm() {
  const doc = docById($("#doc-select").value);
  const all = $("#all-pages").checked;
  const qtype = radio("qtype");
  const count = +$("#count").value;
  return {
    doc_id: doc?.id || "", filename: doc?.filename || "",
    page_from: all ? null : clamp(+$("#page-from").value || 1, 1, doc?.pages || 10000),
    page_to: all ? null : clamp(+$("#page-to").value || 1, 1, doc?.pages || 10000),
    qtype, count,
    theory_count: qtype === "mixed" ? clamp(+$("#theory-count").value || 1, 1, count - 1) : null,
    style: radio("style"), difficulty: radio("difficulty"),
    timing: radio("timing"), minutes: clamp(+$("#minutes").value || 10, 1, 300),
    instant: $("#instant").checked,
    topic: $("#topic").value.trim(),
  };
}
/** Suggested time: about a minute per objective question (less for JAMB's quick-fire style) and several minutes per
 *  theory question (more for university essays). */
function suggestMinutes(f) {
  const nTheory = f.qtype === "theory" ? f.count : f.qtype === "mixed" ? f.theory_count : 0;
  const nMcq = f.count - nTheory;
  const perMcq = f.style === "jamb" ? 0.75 : 1;
  const perTheory = f.style === "university" ? 12 : f.style === "jamb" ? 4 : 8;
  return clamp(Math.ceil(nMcq * perMcq + nTheory * perTheory), 3, 300);
}
function syncForm() {
  const f = readForm();
  const doc = docById(f.doc_id);
  $("#count-out").textContent = f.count;
  $("#theory-count-wrap").hidden = f.qtype !== "mixed";
  $("#theory-count").max = String(f.count - 1);
  if (+$("#theory-count").value > f.count - 1) $("#theory-count").value = String(f.count - 1);
  const all = $("#all-pages").checked;
  $("#page-from").disabled = all; $("#page-to").disabled = all;
  if (doc?.pages) { $("#page-from").max = String(doc.pages); $("#page-to").max = String(doc.pages); }
  $("#minutes-wrap").hidden = f.timing !== "timed";
  if (!state.minutesTouched) $("#minutes").value = String(suggestMinutes(f));
  $("#minutes-hint") && ($("#minutes-hint").textContent = `Suggested: ${suggestMinutes(f)} min`);
  $("#generate").disabled = !doc || doc.status === "stale";
}
function renderDocSelect(selectId) {
  const sel = $("#doc-select");
  const current = selectId || sel.value || prefs().docId;
  const usable = state.docs.filter((d) => d.status !== "stale");
  sel.replaceChildren(...(usable.length
    ? usable.map((d) => el("option", { value: d.id, selected: d.id === current }, `${d.filename} · ${plural(d.pages || 0, "page")}`))
    : [el("option", { value: "" }, "No PDFs yet: upload your notes")]));
  sel.disabled = !usable.length;
  const doc = docById(sel.value);
  if (doc && (selectId || +$("#page-to").value > doc.pages || +$("#page-to").value < 2)) {
    $("#page-from").value = "1"; $("#page-to").value = String(doc.pages || 1);
  }
  const stale = state.docs.filter((d) => d.status === "stale").length;
  $("#doc-hint").textContent = stale
    ? `${plural(stale, "PDF")} in your library need${stale === 1 ? "s" : ""} re-uploading before they can be used.`
    : "Text-based PDFs up to 20 MB. Your PDFs stay saved in this browser and are shared with Chat with PDFs.";
  syncForm();
}
function renderUsage() {
  const u = state.usage;
  if (!u || u.exams_limit == null) return;
  const left = Math.max(0, u.exams_limit - u.exams_used);
  $("#exam-usage").textContent = `${left} of ${u.exams_limit} free exam sets left today`;
}
function applyPrefs() {
  const p = prefs();
  if (p.qtype) setRadio("qtype", p.qtype);
  if (p.count) $("#count").value = String(p.count);
  if (p.theory_count) $("#theory-count").value = String(p.theory_count);
  if (p.style) setRadio("style", p.style);
  if (p.difficulty) setRadio("difficulty", p.difficulty);
  if (p.timing) setRadio("timing", p.timing);
  if (typeof p.instant === "boolean") $("#instant").checked = p.instant;
}

/* ---------------- views ---------------- */
function show(view) {
  state.view = view;
  for (const v of ["setup", "loading", "practice", "results"]) $(`#view-${v}`).hidden = v !== view;
  document.body.dataset.view = view;
  window.scrollTo({ top: 0 });
  if (view === "setup") { renderResume(); renderHistory(); }
}

/* ---------------- generate ---------------- */
async function generate(overrides = {}, { origin = "new" } = {}) {
  const f = { ...readForm(), ...overrides };
  const doc = docById(f.doc_id);
  if (!doc) return toast("Upload or choose a PDF first.", "warn");
  if (f.page_from && f.page_to && f.page_from > f.page_to) return toast("The first page must come before the last page.", "warn");
  if (state.attempt && !state.attempt.submitted && state.attempt.id === lsGet(LS_CURRENT, {})?.id) {
    if (!confirm("You have an unfinished exam. Start a new one and discard it?")) return;
  }
  savePrefs();
  $("#loading-title").textContent = origin === "weak" ? "Writing questions on your weak areas…" : "Writing your exam…";
  $("#loading-text").textContent = `Reading ${f.page_from ? `pages ${f.page_from}–${f.page_to}` : "your notes"} and setting ${plural(f.count, "question")}. This takes 10–60 seconds.`;
  show("loading");
  try {
    await ensureSynced();
    const exam = await api("/api/exam/generate", { method: "POST", body: {
      doc_id: f.doc_id, page_from: f.page_from, page_to: f.page_to, qtype: f.qtype, count: f.count,
      theory_count: f.theory_count, difficulty: f.difficulty, style: f.style, topic: f.topic || null } });
    setUsage(exam.usage);
    startAttempt({
      docId: exam.doc_id, filename: exam.filename || doc.filename, questions: exam.questions, settings: exam.settings,
      timing: f.timing, minutes: f.timing === "timed" ? f.minutes : null, instant: f.instant, origin, topic: f.topic,
    });
    const got = exam.questions.length;
    if (got < f.count) toast(`The AI could only set ${plural(got, "good question")} from these pages.`, "warn", { ms: 7000 });
  } catch (e) {
    show("setup");
    handleError(e);
  }
}

/* ---------------- attempt ---------------- */
function startAttempt({ docId, filename, questions, settings, timing, minutes, instant, origin, topic, parentId }) {
  state.attempt = {
    id: uid(), createdAt: Date.now(), docId, filename, settings, questions,
    timing, minutes, instant, origin: origin || "new", topic: topic || "", parentId: parentId || null,
    answers: {}, marks: {}, flagged: [], current: 0, layout: lsGet(LS_PREFS, {}).layout || "single",
    elapsedMs: 0, submitted: false, finishedAt: null, autoSubmitted: false,
  };
  saveAttempt();
  show("practice");
  renderPractice();
  startTimer();
}
function saveAttempt() {
  const a = state.attempt;
  if (!a) return;
  if (a.submitted) localStorage.removeItem(LS_CURRENT);
  else lsSet(LS_CURRENT, a);
}
const q = (i) => state.attempt.questions[i];
const answered = (qq, att = state.attempt) => {
  const a = att.answers[qq.id];
  return qq.type === "mcq" ? a?.choice != null : !!a?.text?.trim();
};
const maxOf = (qq) => (qq.type === "mcq" ? 1 : qq.marks);

/* ---------------- timer ---------------- */
function startTimer() {
  stopTimer();
  state.lastTick = Date.now();
  state.timer = setInterval(tick, 500);
  tick();
}
function stopTimer() { if (state.timer) clearInterval(state.timer); state.timer = null; }
let lastSaved = 0;
function tick() {
  const a = state.attempt;
  if (!a || a.submitted) return stopTimer();
  const now = Date.now();
  if (!document.hidden || a.timing === "timed") a.elapsedMs += now - state.lastTick;  // untimed: don't count time away
  state.lastTick = now;
  const timer = $("#timer");
  if (a.timing === "timed") {
    const left = a.minutes * 60e3 - a.elapsedMs;
    $("#timer-text").textContent = fmtTime(left);
    timer.classList.toggle("low", left <= 60e3);
    timer.title = "Time left";
    if (left <= 0) { a.autoSubmitted = true; submitAttempt({ auto: true }); return; }
  } else {
    $("#timer-text").textContent = fmtTime(a.elapsedMs);
    timer.classList.remove("low");
    timer.title = "Time spent";
  }
  if (now - lastSaved > 5000) { lastSaved = now; saveAttempt(); }
}

/* ---------------- practice rendering ---------------- */
function renderPractice() {
  const a = state.attempt;
  const s = a.settings || {};
  $("#p-title").textContent = a.origin === "retry" ? "Retry: wrong questions" : `${STYLE_NAMES[s.style] || "Practice"} · ${QTYPE_NAMES[s.qtype] || ""}`;
  const range = s.page_from ? `pp. ${s.page_from}–${s.page_to}` : "";
  $("#p-sub").textContent = [a.filename, range, a.timing === "timed" ? `${a.minutes} min` : "untimed"].filter(Boolean).join(" · ");
  const single = a.layout === "single";
  $("#layout-btn").replaceChildren(icon(single ? "list" : "single"));
  $("#layout-btn").title = single ? "Show all questions on one page" : "Show one question at a time";
  $("#pnav").hidden = !single;
  renderNavigator();
  const box = $("#questions");
  if (single) {
    a.current = clamp(a.current, 0, a.questions.length - 1);
    box.replaceChildren(renderQuestion(a.current));
    $("#prev-q").disabled = a.current === 0;
    const last = a.current === a.questions.length - 1;
    $("#next-q").replaceChildren(last ? "Finish" : "Next", icon(last ? "check" : "right"));
    $("#pnav-label").textContent = `${a.current + 1} of ${a.questions.length}`;
  } else {
    box.replaceChildren(...a.questions.map((_, i) => renderQuestion(i)));
  }
}
function navState(qq) {
  const m = state.attempt.marks[qq.id];
  if (m) return m.score >= m.max ? "right" : m.score > 0 ? "part" : "wrong";
  return answered(qq) ? "done" : "";
}
function renderNavigator() {
  const a = state.attempt;
  const nav = $("#navigator");
  nav.replaceChildren(...a.questions.map((qq, i) => el("button", {
    type: "button",
    class: ["nav-q", navState(qq), a.flagged.includes(qq.id) ? "flag" : "", a.layout === "single" && i === a.current ? "current" : ""].filter(Boolean).join(" "),
    "aria-label": `Question ${i + 1}${answered(qq) ? ", answered" : ""}${a.flagged.includes(qq.id) ? ", flagged" : ""}`,
    onclick: () => goTo(i),
  }, String(i + 1))));
  const done = a.questions.filter(answered).length;
  nav.append(el("span", { class: "nav-count muted" }, `${done}/${a.questions.length} answered`));
}
function goTo(i) {
  const a = state.attempt;
  if (a.layout === "single") { a.current = i; saveAttempt(); renderPractice(); window.scrollTo({ top: 0 }); }
  else document.getElementById(`qc-${a.questions[i].id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
}
function refreshQuestion(i) {
  const a = state.attempt;
  const old = document.getElementById(`qc-${a.questions[i].id}`);
  if (old) old.replaceWith(renderQuestion(i));
  renderNavigator();
}
function renderQuestion(i) {
  const a = state.attempt;
  const qq = q(i);
  const flagged = a.flagged.includes(qq.id);
  const head = el("div", { class: "q-head" },
    el("span", { class: "q-num" }, `Question ${i + 1}`),
    el("span", { class: "q-tags" },
      el("span", { class: "tag" }, qq.type === "mcq" ? "Objective · 1 mark" : `Theory · ${plural(qq.marks, "mark")}`),
      qq.topic ? el("span", { class: "tag soft" }, qq.topic) : null,
      el("span", { class: `tag diff ${qq.difficulty}` }, qq.difficulty)),
    el("button", { type: "button", class: `act flag-btn${flagged ? " on" : ""}`, "aria-pressed": String(flagged), title: "Flag to come back to", onclick: () => toggleFlag(i) },
      icon("flag"), el("span", {}, flagged ? "Flagged" : "Flag")));
  const card = el("article", { class: "ex-card q-card", id: `qc-${qq.id}` }, head, md(qq.question, false, "div", "md q-stem"));
  card.append(qq.type === "mcq" ? renderMcq(i) : renderTheory(i));
  return card;
}
function renderMcq(i) {
  const a = state.attempt;
  const qq = q(i);
  const ans = a.answers[qq.id];
  const mark = a.marks[qq.id];
  const locked = !!mark;
  const list = el("div", { class: "options", role: "radiogroup", "aria-label": `Options for question ${i + 1}` });
  qq.options.forEach((opt, k) => {
    const cls = ["opt"];
    if (ans?.choice === k) cls.push("picked");
    if (locked && k === qq.answer) cls.push("right");
    if (locked && ans?.choice === k && k !== qq.answer) cls.push("wrong");
    list.append(el("button", { type: "button", class: cls.join(" "), role: "radio", "aria-checked": String(ans?.choice === k), disabled: locked || null,
      onclick: () => pickOption(i, k) },
      el("span", { class: "opt-letter" }, LETTERS[k]), md(opt, true, "span", "md opt-text")));
  });
  const wrap = el("div", { class: "mcq" }, list);
  if (locked) wrap.append(feedbackMcq(qq, mark));
  return wrap;
}
function feedbackMcq(qq, mark) {
  const ok = mark.score >= 1;
  return el("div", { class: `feedback ${ok ? "ok" : "bad"}` },
    el("div", { class: "fb-title" }, icon(ok ? "check" : "x"), ok ? "Correct" : mark.choice == null ? `Not answered · the answer is ${LETTERS[qq.answer]}` : `Not quite · the answer is ${LETTERS[qq.answer]}`),
    qq.explanation ? md(qq.explanation) : null,
    pageLinks(qq.pages, state.attempt.docId));
}
function pickOption(i, k) {
  const a = state.attempt;
  const qq = q(i);
  if (a.marks[qq.id] || a.submitted) return;
  a.answers[qq.id] = { choice: k };
  if (a.instant) a.marks[qq.id] = markMcqLocal(qq, k);
  saveAttempt();
  refreshQuestion(i);
}
function markMcqLocal(qq, choice) {
  const ok = choice != null && choice === qq.answer;
  return { type: "mcq", correct: ok, choice: choice ?? null, answer: qq.answer, score: ok ? 1 : 0, max: 1, explanation: qq.explanation, pages: qq.pages };
}
function renderTheory(i) {
  const a = state.attempt;
  const qq = q(i);
  const mark = a.marks[qq.id];
  const wrap = el("div", { class: "theory" });
  if (qq.parts?.length) {
    wrap.append(el("ol", { class: "parts" }, qq.parts.map((p) => el("li", {},
      el("span", { class: "part-label" }, `(${p.label})`), md(p.text, true, "span", "md part-text"), el("span", { class: "part-marks" }, `[${p.marks}]`)))));
  }
  const ta = el("textarea", { class: "answer-box", rows: "7", maxlength: "6000", placeholder: qq.parts?.length ? "Write your answer. Label each part: (a) …, (b) …" : "Write your answer as you would in the exam.", "aria-label": `Your answer to question ${i + 1}`, readonly: mark ? true : null });
  ta.value = a.answers[qq.id]?.text || "";
  const count = el("span", { class: "muted wc" });
  const updateCount = () => { const w = ta.value.trim() ? ta.value.trim().split(/\s+/).length : 0; count.textContent = plural(w, "word"); };
  updateCount();
  let t;
  ta.addEventListener("input", () => {
    a.answers[qq.id] = { text: ta.value };
    updateCount();
    clearTimeout(t); t = setTimeout(() => { saveAttempt(); renderNavigator(); }, 400);
  });
  wrap.append(ta);
  const foot = el("div", { class: "theory-foot" }, count);
  if (!mark && a.instant) {
    const btn = el("button", { type: "button", class: "btn btn-sm", onclick: () => markOne(i, btn) }, icon("sparkle"), "Mark my answer");
    foot.append(btn);
  } else if (mark && !a.submitted) {
    foot.append(el("button", { type: "button", class: "btn btn-ghost btn-sm", onclick: () => { delete a.marks[qq.id]; saveAttempt(); refreshQuestion(i); } }, icon("refresh"), "Edit and re-mark"));
  }
  wrap.append(foot);
  if (mark) wrap.append(feedbackTheory(qq, mark));
  return wrap;
}
function feedbackTheory(qq, mark) {
  const p = pct(mark.score, mark.max);
  const cls = p >= 70 ? "ok" : p >= 40 ? "part" : "bad";
  const box = el("div", { class: `feedback ${cls}` },
    el("div", { class: "fb-title" }, icon(p >= 70 ? "check" : "sparkle"), `${mark.score} / ${mark.max} marks`, mark.unmarked ? el("span", { class: "muted" }, " · not marked yet") : null));
  if (mark.feedback) box.append(md(mark.feedback, false, "div", "md fb-text"));
  if (mark.points?.length) {
    box.append(el("ul", { class: "points" }, mark.points.map((pt) => el("li", { class: pt.awarded >= pt.marks ? "got" : pt.awarded > 0 ? "half" : "miss" },
      el("span", { class: "pt-mark" }, `${pt.awarded}/${pt.marks}`), el("span", {}, md(pt.point, true, "span", "md"), pt.comment ? el("span", { class: "muted pt-comment" }, ` · ${pt.comment}`) : null)))));
  }
  if (mark.good?.length) box.append(el("div", { class: "fb-list good" }, el("b", {}, "What you did well"), el("ul", {}, mark.good.map((g) => el("li", {}, g)))));
  if (mark.missing?.length) box.append(el("div", { class: "fb-list miss" }, el("b", {}, "To score higher"), el("ul", {}, mark.missing.map((g) => el("li", {}, g)))));
  const model = el("details", { class: "model" }, el("summary", {}, "Model answer"), md(mark.model_answer || qq.model_answer));
  box.append(model, pageLinks(qq.pages, state.attempt.docId));
  return box;
}
function toggleFlag(i) {
  const a = state.attempt;
  const id = q(i).id;
  a.flagged = a.flagged.includes(id) ? a.flagged.filter((x) => x !== id) : [...a.flagged, id];
  saveAttempt();
  refreshQuestion(i);
}

/* ---------------- marking ---------------- */
async function markTheory(items) {
  // items: [{id, question, answer}] -> results by id. Batches of MARK_BATCH.
  const out = {};
  for (let k = 0; k < items.length; k += MARK_BATCH) {
    const batch = items.slice(k, k + MARK_BATCH);
    await ensureSynced();
    const r = await api("/api/exam/mark", { method: "POST", body: { style: state.attempt.settings?.style || "general", items: batch } });
    setUsage(r.usage);
    Object.assign(out, r.results);
  }
  return out;
}
async function markOne(i, btn) {
  const a = state.attempt;
  const qq = q(i);
  const text = a.answers[qq.id]?.text || "";
  if (text.trim().length < 2) return toast("Write your answer first.", "warn");
  btn.disabled = true; btn.replaceChildren(el("span", { class: "thinking sm" }, el("i"), el("i"), el("i")), "Marking…");
  try {
    const r = await markTheory([{ id: qq.id, question: qq, answer: text }]);
    if (r[qq.id]) a.marks[qq.id] = r[qq.id];
    saveAttempt();
    refreshQuestion(i);
  } catch (e) {
    handleError(e);
    btn.disabled = false; btn.replaceChildren(icon("sparkle"), "Mark my answer");
  }
}
function unmarkedResult(qq) {
  return { type: "theory", score: 0, max: qq.marks, unmarked: true, points: [], good: [], missing: [],
    feedback: "This answer couldn't be marked by the AI just now. Compare it with the model answer, or use Mark again.", model_answer: qq.model_answer, pages: qq.pages };
}
async function confirmSubmit() {
  const a = state.attempt;
  const left = a.questions.filter((qq) => !answered(qq)).length;
  const flagged = a.flagged.length;
  $("#confirm-title").textContent = "Submit exam?";
  $("#confirm-text").textContent = [
    left ? `${plural(left, "question")} still unanswered.` : "Every question is answered.",
    flagged ? `${plural(flagged, "question")} flagged for review.` : "",
    "Unmarked written answers are marked by the AI when you submit.",
  ].filter(Boolean).join(" ");
  const dlg = $("#dlg-confirm");
  dlg.returnValue = "";
  dlg.showModal();
  dlg.addEventListener("close", function once() {
    dlg.removeEventListener("close", once);
    if (dlg.returnValue === "ok") submitAttempt();
  });
}
let submitting = false;
async function submitAttempt({ auto = false } = {}) {
  const a = state.attempt;
  if (!a || a.submitted || submitting) return;
  submitting = true;
  stopTimer();
  if (auto) toast("Time's up. Your exam was submitted.", "warn", { ms: 6000 });
  for (const qq of a.questions) if (qq.type === "mcq" && !a.marks[qq.id]) a.marks[qq.id] = markMcqLocal(qq, a.answers[qq.id]?.choice ?? null);
  const todo = a.questions.filter((qq) => qq.type === "theory" && (!a.marks[qq.id] || a.marks[qq.id].unmarked));
  if (todo.length) {
    $("#loading-title").textContent = "Marking your answers…";
    $("#loading-text").textContent = `The AI examiner is marking ${plural(todo.length, "written answer")} against the marking scheme.`;
    show("loading");
    try {
      const r = await markTheory(todo.map((qq) => ({ id: qq.id, question: qq, answer: a.answers[qq.id]?.text || "" })));
      for (const qq of todo) a.marks[qq.id] = r[qq.id] || unmarkedResult(qq);
    } catch (e) {
      handleError(e);
      for (const qq of todo) a.marks[qq.id] = unmarkedResult(qq);
    }
  }
  a.submitted = true;
  a.finishedAt = Date.now();
  saveAttempt();
  saveHistory(a);
  submitting = false;
  renderResults(a);
}

/* ---------------- results ---------------- */
function summarize(a) {
  let score = 0, max = 0, right = 0, wrong = 0, blank = 0, unmarked = 0;
  const topics = {}, pages = {};
  for (const qq of a.questions) {
    const m = a.marks[qq.id] || { score: 0, max: maxOf(qq) };
    score += m.score; max += m.max;
    if (m.unmarked) unmarked++;
    if (!answered(qq, a)) blank++;
    else if (m.score >= m.max) right++;
    else wrong++;
    const t = qq.topic || "General";
    topics[t] ||= { score: 0, max: 0, n: 0, pages: new Set() };
    topics[t].score += m.score; topics[t].max += m.max; topics[t].n++;
    for (const p of qq.pages || []) {
      topics[t].pages.add(p);
      pages[p] ||= { score: 0, max: 0, n: 0 };
      pages[p].score += m.score; pages[p].max += m.max; pages[p].n++;
    }
  }
  const weak = Object.entries(topics)
    .map(([name, t]) => ({ name, pct: pct(t.score, t.max), pages: [...t.pages].sort((x, y) => x - y), n: t.n }))
    .filter((t) => t.pct < WEAK_PCT).sort((x, y) => x.pct - y.pct);
  return { score: Math.round(score * 2) / 2, max, pct: pct(score, max), right, wrong, blank, unmarked, topics, pages, weak };
}
function bars(entries, labelFn) {
  return el("div", { class: "bars" }, entries.map(([key, t]) => {
    const p = pct(t.score, t.max);
    return el("div", { class: "bar-row" },
      el("span", { class: "bar-label" }, labelFn(key, t)),
      el("span", { class: "bar" }, el("span", { class: `bar-fill ${p >= 70 ? "ok" : p >= WEAK_PCT ? "part" : "bad"}`, style: `width:${Math.max(3, p)}%` })),
      el("span", { class: "bar-pct" }, `${p}%`));
  }));
}
function verdict(p) {
  if (p >= 85) return "Excellent. You're exam-ready on this material.";
  if (p >= 70) return "Strong work. A little revision on the weak spots and you're there.";
  if (p >= 50) return "A solid base. Revise the pages below, then try again.";
  return "Good that you practised now and not in the exam hall. Revise the pages below and retry.";
}
function renderResults(a) {
  stopTimer();
  state.attempt = a;
  const s = summarize(a);
  const box = $("#results");
  const timeUsed = fmtDuration(a.elapsedMs);
  const wrongQs = a.questions.filter((qq) => { const m = a.marks[qq.id]; return !m || m.score < m.max; });
  const hero = el("div", { class: "ex-card res-hero" },
    el("div", { class: `ring ${s.pct >= 70 ? "ok" : s.pct >= WEAK_PCT ? "part" : "bad"}`, style: `--p:${s.pct}` }, el("b", {}, `${s.pct}%`)),
    el("div", { class: "res-main" },
      el("h1", {}, verdict(s.pct)),
      el("p", { class: "muted" }, `${s.score} of ${plural(s.max, "mark")} · ${timeUsed}${a.timing === "timed" ? ` of ${a.minutes} min` : ""}${a.autoSubmitted ? " · auto-submitted" : ""}`),
      el("div", { class: "stats" },
        el("span", { class: "stat ok" }, el("b", {}, s.right), " full marks"),
        el("span", { class: "stat bad" }, el("b", {}, s.wrong), " lost marks"),
        el("span", { class: "stat" }, el("b", {}, s.blank), " unanswered")),
      el("p", { class: "muted small" }, `${a.filename}${a.settings?.page_from ? ` · pages ${a.settings.page_from}–${a.settings.page_to}` : ""} · ${STYLE_NAMES[a.settings?.style] || ""} · ${fmtDate(a.finishedAt || a.createdAt)}`)));
  const actions = el("div", { class: "res-actions" },
    wrongQs.length ? el("button", { class: "btn btn-primary", onclick: () => retryWrong(a) }, icon("refresh"), `Retry wrong questions (${wrongQs.length})`) : null,
    el("button", { class: s.weak.length ? "btn" : "btn", onclick: () => generateWeak(a, s) }, icon("sparkle"), s.weak.length ? "Generate new set on weak areas" : "Generate a fresh set"),
    el("button", { class: "btn btn-ghost", onclick: () => { state.attempt = null; show("setup"); } }, icon("left"), "New exam"));
  if (s.unmarked) {
    actions.prepend(el("button", { class: "btn", onclick: (e) => remarkUnmarked(a, e.currentTarget) }, icon("sparkle"), `Mark again (${s.unmarked})`));
  }
  const weakCard = el("div", { class: "ex-card weak" }, el("h2", {}, "Weak areas"),
    s.weak.length
      ? el("ul", { class: "weak-list" }, s.weak.slice(0, 5).map((w) => el("li", {},
          el("span", { class: "weak-name" }, w.name), el("span", { class: "weak-pct" }, `${w.pct}%`),
          w.pages.length ? el("span", { class: "muted" }, " · revise ", w.pages.map((p, i) => [i ? ", " : "", el("button", { class: "linkish", onclick: () => openPage(a.docId, p) }, `p. ${p}`)])) : null)))
      : el("p", { class: "muted" }, "No weak topics this time: every topic scored 60% or more."));
  const topicEntries = Object.entries(s.topics).sort((x, y) => pct(x[1].score, x[1].max) - pct(y[1].score, y[1].max));
  const pageEntries = Object.entries(s.pages).sort((x, y) => +x[0] - +y[0]);
  const breakdown = el("div", { class: "ex-card breakdown" },
    el("div", { class: "bd-col" }, el("h2", {}, "By topic"), bars(topicEntries, (k, t) => `${k} (${t.n})`)),
    pageEntries.length ? el("div", { class: "bd-col" }, el("h2", {}, "By page"), bars(pageEntries, (k) => el("button", { class: "linkish", onclick: () => openPage(a.docId, +k) }, `Page ${k}`))) : null);
  const review = el("div", { class: "review" }, el("h2", { class: "review-title" }, "Review every question"),
    a.questions.map((qq, i) => reviewCard(a, qq, i)));
  box.replaceChildren(hero, actions, el("div", { class: "res-grid" }, weakCard, breakdown), review);
  show("results");
}
function reviewCard(a, qq, i) {
  const m = a.marks[qq.id] || (qq.type === "mcq" ? markMcqLocal(qq, null) : unmarkedResult(qq));
  const ok = m.score >= m.max;
  const card = el("article", { class: `ex-card q-card review-card ${ok ? "is-ok" : m.score > 0 ? "is-part" : "is-bad"}` },
    el("div", { class: "q-head" }, el("span", { class: "q-num" }, `Question ${i + 1}`),
      el("span", { class: "q-tags" }, el("span", { class: "tag" }, `${m.score}/${m.max}`), qq.topic ? el("span", { class: "tag soft" }, qq.topic) : null)),
    md(qq.question, false, "div", "md q-stem"));
  if (qq.type === "mcq") {
    const choice = a.answers[qq.id]?.choice;
    card.append(el("div", { class: "options" }, qq.options.map((opt, k) => el("div", {
      class: ["opt", "static", k === qq.answer ? "right" : "", choice === k && k !== qq.answer ? "wrong" : "", choice === k ? "picked" : ""].filter(Boolean).join(" ") },
      el("span", { class: "opt-letter" }, LETTERS[k]), md(opt, true, "span", "md opt-text")))));
    card.append(feedbackMcq(qq, m));
  } else {
    if (qq.parts?.length) card.append(el("ol", { class: "parts" }, qq.parts.map((p) => el("li", {}, el("span", { class: "part-label" }, `(${p.label})`), md(p.text, true, "span", "md part-text"), el("span", { class: "part-marks" }, `[${p.marks}]`)))));
    const text = a.answers[qq.id]?.text?.trim();
    card.append(el("div", { class: "your-answer" }, el("b", {}, "Your answer"), text ? el("p", { class: "pre" }, text) : el("p", { class: "muted" }, "No answer written.")));
    card.append(feedbackTheory(qq, m));
  }
  return card;
}
async function remarkUnmarked(a, btn) {
  const todo = a.questions.filter((qq) => a.marks[qq.id]?.unmarked);
  btn.disabled = true;
  try {
    const r = await markTheory(todo.map((qq) => ({ id: qq.id, question: qq, answer: a.answers[qq.id]?.text || "" })));
    for (const qq of todo) if (r[qq.id]) a.marks[qq.id] = r[qq.id];
    saveHistory(a);
    renderResults(a);
  } catch (e) { handleError(e); btn.disabled = false; }
}
function retryWrong(a) {
  const wrongQs = a.questions.filter((qq) => { const m = a.marks[qq.id]; return !m || m.score < m.max; })
    .map((qq, i) => ({ ...qq, id: `r${i + 1}` }));
  if (!wrongQs.length) return;
  const nTheory = wrongQs.filter((x) => x.type === "theory").length;
  const minutes = a.timing === "timed" ? clamp(Math.ceil(wrongQs.length - nTheory + nTheory * 8), 2, 300) : null;
  startAttempt({ docId: a.docId, filename: a.filename, questions: wrongQs, settings: { ...a.settings, count: wrongQs.length },
    timing: a.timing, minutes, instant: a.instant, origin: "retry", parentId: a.id });
}
function generateWeak(a, s) {
  const st = a.settings || {};
  if (!docById(a.docId)) return toast("That PDF is no longer in your library. Upload it again to make a new set.", "warn", { ms: 7000 });
  const topic = s.weak.length ? s.weak.slice(0, 4).map((w) => w.name).join(", ") : (a.topic || "");
  const weakPages = s.weak.flatMap((w) => w.pages);
  let from = st.page_from || null, to = st.page_to || null;
  if (weakPages.length) {  // narrow to the pages the weak topics come from, with a page of context either side
    const doc = docById(a.docId);
    from = Math.max(st.page_from || 1, Math.min(...weakPages) - 1);
    to = Math.min(st.page_to || doc?.pages || 10000, Math.max(...weakPages) + 1);
  }
  const count = clamp(st.count || 10, 5, 50);
  const qtype = st.qtype || "mcq";
  generate({ doc_id: a.docId, page_from: from, page_to: to, qtype, count,
    theory_count: qtype === "mixed" ? clamp(st.n_theory || 1, 1, count - 1) : null,
    style: st.style || "general", difficulty: st.difficulty || "mixed", topic,
    timing: a.timing, minutes: a.timing === "timed" ? (a.minutes || 10) : 10, instant: a.instant }, { origin: "weak" });
}

/* ---------------- progress history ---------------- */
function saveHistory(a) {
  const s = summarize(a);
  const entry = {
    id: a.id, date: a.finishedAt || Date.now(), docId: a.docId, filename: a.filename, origin: a.origin,
    style: a.settings?.style, qtype: a.settings?.qtype, n: a.questions.length, score: s.score, max: s.max, pct: s.pct,
    timeMs: a.elapsedMs, weak: s.weak.slice(0, 3).map((w) => w.name),
    attempt: { ...a },
  };
  let hist = lsGet(LS_HISTORY, []).filter((h) => h.id !== a.id);
  hist.unshift(entry);
  hist = hist.slice(0, MAX_HISTORY);
  while (!lsSet(LS_HISTORY, hist) && hist.length) {  // storage full: keep summaries, drop full attempts from the oldest
    const full = [...hist].reverse().find((h) => h.attempt);
    if (full) delete full.attempt; else hist.pop();
  }
}
function renderHistory() {
  const hist = lsGet(LS_HISTORY, []);
  const body = $("#history-body");
  $("#clear-history").hidden = !hist.length;
  if (!hist.length) {
    body.replaceChildren(el("p", { class: "muted" }, "Your scores will appear here after your first exam, so you can watch yourself improve."));
    return;
  }
  const recent = hist.slice(0, 12).reverse();
  const avg = Math.round(hist.slice(0, 10).reduce((t, h) => t + h.pct, 0) / Math.min(10, hist.length));
  const best = Math.max(...hist.map((h) => h.pct));
  const spark = el("div", { class: "spark", "aria-label": "Recent scores" }, recent.map((h) => el("span", { class: `spark-bar ${h.pct >= 70 ? "ok" : h.pct >= WEAK_PCT ? "part" : "bad"}`, style: `height:${Math.max(6, h.pct)}%`, title: `${h.pct}% · ${fmtDate(h.date)}` })));
  const summary = el("div", { class: "hist-sum" },
    el("div", {}, el("b", {}, `${hist.length}`), el("span", { class: "muted" }, hist.length === 1 ? " exam taken" : " exams taken")),
    el("div", {}, el("b", {}, `${avg}%`), el("span", { class: "muted" }, " recent average")),
    el("div", {}, el("b", {}, `${best}%`), el("span", { class: "muted" }, " best")), spark);
  const rows = el("ul", { class: "hist-list" }, hist.slice(0, 10).map((h) => el("li", {},
    el("span", { class: `hist-pct ${h.pct >= 70 ? "ok" : h.pct >= WEAK_PCT ? "part" : "bad"}` }, `${h.pct}%`),
    el("span", { class: "hist-main" },
      el("span", { class: "hist-name" }, h.filename || "Exam"),
      el("span", { class: "muted small" }, [h.origin === "retry" ? "Retry" : h.origin === "weak" ? "Weak areas" : STYLE_NAMES[h.style], QTYPE_NAMES[h.qtype], plural(h.n, "question"), fmtDuration(h.timeMs || 0), fmtDate(h.date)].filter(Boolean).join(" · ")),
      h.weak?.length ? el("span", { class: "muted small" }, `Weak: ${h.weak.join(", ")}`) : null),
    h.attempt ? el("button", { class: "btn btn-ghost btn-sm", onclick: () => renderResults(h.attempt) }, "Review") : null)));
  body.replaceChildren(summary, rows);
}
function renderResume() {
  const box = $("#resume");
  const cur = lsGet(LS_CURRENT, null);
  if (!cur || cur.submitted || !cur.questions?.length) { box.hidden = true; return; }
  const done = cur.questions.filter((qq) => (qq.type === "mcq" ? cur.answers[qq.id]?.choice != null : !!cur.answers[qq.id]?.text?.trim())).length;
  const left = cur.timing === "timed" ? ` · ${fmtTime(cur.minutes * 60e3 - cur.elapsedMs)} left` : "";
  box.hidden = false;
  box.replaceChildren(
    el("div", {}, el("b", {}, "Unfinished exam"), el("span", { class: "muted" }, ` · ${cur.filename} · ${done}/${cur.questions.length} answered${left}`)),
    el("div", { class: "resume-acts" },
      el("button", { class: "btn btn-ghost btn-sm", onclick: () => { if (confirm("Discard this unfinished exam?")) { localStorage.removeItem(LS_CURRENT); state.attempt = null; renderResume(); } } }, "Discard"),
      el("button", { class: "btn btn-primary btn-sm", onclick: () => resume(cur) }, "Resume")));
}
function resume(cur) {
  state.attempt = cur;
  show("practice");
  renderPractice();
  startTimer();
}

/* ---------------- theme ---------------- */
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $("#theme-btn").replaceChildren(icon(t === "light" ? "sun" : t === "dark" ? "moon" : "auto"));
  $("#theme-btn").title = `Theme: ${t}`;
  localStorage.setItem("dc-theme", t);
}

/* ---------------- events ---------------- */
function bindEvents() {
  $("#theme-btn").addEventListener("click", () => applyTheme(THEMES[(THEMES.indexOf(localStorage.getItem("dc-theme") || "auto") + 1) % 3]));
  $("#setup-form").addEventListener("input", (e) => { if (e.target.id === "minutes") state.minutesTouched = true; syncForm(); });
  $("#setup-form").addEventListener("change", (e) => { if (e.target.id === "doc-select") renderDocSelect(e.target.value); syncForm(); });
  $("#setup-form").addEventListener("submit", (e) => { e.preventDefault(); generate(); });
  $("#file-input").addEventListener("change", (e) => { const f = e.target.files?.[0]; if (f && !state.uploading) uploadFile(f); });
  $("#upload-label").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file-input").click(); } });
  $("#clear-history").addEventListener("click", () => { if (confirm("Clear your exam history on this device?")) { localStorage.removeItem(LS_HISTORY); renderHistory(); } });
  $("#submit-btn").addEventListener("click", confirmSubmit);
  $("#layout-btn").addEventListener("click", () => {
    const a = state.attempt;
    a.layout = a.layout === "single" ? "all" : "single";
    lsSet(LS_PREFS, { ...prefs(), layout: a.layout });
    saveAttempt(); renderPractice();
  });
  $("#prev-q").addEventListener("click", () => goTo(Math.max(0, state.attempt.current - 1)));
  $("#next-q").addEventListener("click", () => {
    const a = state.attempt;
    if (a.current >= a.questions.length - 1) confirmSubmit(); else goTo(a.current + 1);
  });
  document.addEventListener("keydown", (e) => {
    if (state.view !== "practice" || state.attempt?.layout !== "single") return;
    if (/^(TEXTAREA|INPUT|SELECT)$/.test(e.target.tagName) || document.querySelector("dialog[open]")) return;
    const qq = q(state.attempt.current);
    if (e.key === "ArrowRight") goTo(Math.min(state.attempt.questions.length - 1, state.attempt.current + 1));
    else if (e.key === "ArrowLeft") goTo(Math.max(0, state.attempt.current - 1));
    else if (qq.type === "mcq" && /^[a-eA-E1-5]$/.test(e.key)) {
      const k = /\d/.test(e.key) ? +e.key - 1 : "abcde".indexOf(e.key.toLowerCase());
      if (k < qq.options.length) pickOption(state.attempt.current, k);
    }
  });
  document.addEventListener("visibilitychange", () => { state.lastTick = Date.now(); if (document.hidden) saveAttempt(); });
  window.addEventListener("pagehide", saveAttempt);
  $("#dlg-page").addEventListener("click", (e) => { if (e.target === $("#dlg-page")) $("#dlg-page").close(); });
}

/* ---------------- boot ---------------- */
async function boot() {
  applyTheme(localStorage.getItem("dc-theme") || "auto");
  bindEvents();
  applyPrefs();
  await loadDocs();
  renderDocSelect();
  show("setup");
  try { state.config = await api("/api/config"); }
  catch (e) { handleError(e); }
  try { setUsage(await api("/api/usage")); } catch { /* offline: keep going */ }
  await syncDocs();
  if (new URLSearchParams(location.search).get("resume") === "1") { const cur = lsGet(LS_CURRENT, null); if (cur && !cur.submitted) resume(cur); }
}
boot().catch((e) => { console.error(e); toast("Exam Prep failed to start. Please reload the page.", "error", { ms: 10000 }); });
