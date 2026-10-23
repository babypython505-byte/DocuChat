// DocuChat app. Vanilla ES module; markdown (marked), sanitising (DOMPurify), math (KaTeX) and pdf.js load from jsDelivr.
// Persistence: conversations and documents (PDF blob + chunks + vectors) live in IndexedDB. The server is stateless
// across restarts, so on boot (and whenever the API answers 409) the browser re-hydrates its workspace.

const PDFJS = "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.10.38/build";
const HISTORY_TURNS = 6;
const HISTORY_CHARS = 1200;

/* ---------------- utilities ---------------- */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
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
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
function plural(n, w) { return `${n} ${w}${n === 1 ? "" : "s"}`; }
function download(name, text, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = el("a", { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = el("textarea", { style: "position:fixed;opacity:0" }); ta.value = text; document.body.append(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  if (btn) {
    btn.classList.add("done"); const label = btn.querySelector("span"); const old = label?.textContent;
    if (label) label.textContent = "Copied";
    setTimeout(() => { btn.classList.remove("done"); if (label) label.textContent = old; }, 1400);
  }
}

/* ---------------- IndexedDB ---------------- */
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
    del: (s, k) => run(s, "readwrite", (st) => st.delete(k)),
  };
})();
const safeIdb = async (fn, fallback) => { try { return await fn(); } catch (e) { console.warn("storage:", e?.message || e); return fallback; } };

/* ---------------- state ---------------- */
const state = {
  config: null,
  usage: null,
  docs: [],          // {id, filename, pages, chunks, size, addedAt, meta, status: ready|syncing|stale|serverOnly}
  uploads: [],       // {tmp, name, progress, phase}
  selected: new Set(JSON.parse(localStorage.getItem("dc-selected") || "null") || []),
  selectAll: localStorage.getItem("dc-selected") === null,
  convs: [],
  current: null,
  busy: false,
  abort: null,
  search: "",
  lastOk: 0,         // last successful API response; after a long gap the server may have restarted (Render sleeps)
};
const SYNC_STALE_MS = 3 * 60e3;
const saveSelection = () => localStorage.setItem("dc-selected", JSON.stringify([...state.selected]));
const readyDocs = () => state.docs.filter((d) => d.status !== "stale");
const scopeDocs = () => readyDocs().filter((d) => state.selected.has(d.id));
const docById = (id) => state.docs.find((d) => d.id === id);
function newConv() { return { id: uid(), title: "New chat", createdAt: Date.now(), updatedAt: Date.now(), messages: [] }; }
async function saveConv(conv = state.current) {
  if (!conv || !conv.messages.length) return;
  conv.updatedAt = Date.now();
  if (!state.convs.includes(conv)) state.convs.unshift(conv);
  if (conv === state.current) localStorage.setItem("dc-last", conv.id);
  await safeIdb(() => idb.put("convs", JSON.parse(JSON.stringify(conv))));
  renderConvs();
  if (!state.busy) pushChat(conv);
}
/** Signed in: keep chats in the account too (debounced per chat; no-op for guests). */
const pushTimers = new Map();
function pushChat(conv) {
  if (!window.dcAuth?.user) return;
  clearTimeout(pushTimers.get(conv.id));
  pushTimers.set(conv.id, setTimeout(() => { pushTimers.delete(conv.id); window.dcAuth.db.saveChat(conv); }, 800));
}

/* ---------------- API ---------------- */
class ApiError extends Error {
  constructor(status, detail, body) { super(detail); this.status = status; this.body = body; }
}
async function toApiError(res) {
  let body = {};
  try { body = await res.json(); } catch { /* non-JSON error */ }
  let detail = body.detail;
  if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
  return new ApiError(res.status, detail || res.statusText || "Request failed", body);
}
async function api(path, { method = "GET", body, signal, retry = true } = {}) {
  let res;
  try {
    res = await fetch(path, {
      method, signal, credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError(0, "Can't reach DocuChat. Check your connection; the server may be waking up (about 30 seconds).");
  }
  if (res.status === 409 && retry) {
    const err = await toApiError(res);
    if (err.body?.code === "missing_documents") {
      await restoreMany(err.body.missing || []);
      return api(path, { method, body, signal, retry: false });
    }
    throw err;
  }
  if (!res.ok) throw await toApiError(res);
  state.lastOk = Date.now();
  return res;
}
/** Before asking after a long idle gap, re-sync documents first so a restarted server is re-hydrated up front
 *  instead of answering 409 and retrying. The 409 path stays as the fallback. */
async function ensureSynced() {
  if (Date.now() - state.lastOk > SYNC_STALE_MS) await syncDocs({ silent: true });
}
const apiJson = async (path, opts) => (await api(path, opts)).json();

const SIGNUP_LINK = () => ({ href: `/login?tab=signup&next=${encodeURIComponent(location.pathname + location.search)}`, text: "Create free account" });
const isGuest = () => state.usage && state.usage.signed_in === false;
function handleError(err, context = "") {
  if (err?.name === "AbortError") return;
  const s = err?.status;
  const msg = err?.message || "Something went wrong.";
  if ((s === 429 || s === 402) && isGuest()) toast(msg, "warn", { link: SIGNUP_LINK(), ms: 12000 });
  else if (s === 429) toast(`${msg}`, "error", { link: { href: "mailto:babypython505@gmail.com?subject=DocuChat%20Pro", text: "Join Pro waitlist" }, ms: 9000 });
  else if (s === 402) toast(msg, "warn", { ms: 7000 });
  else if (s === 503 || s === 502) toast(msg.replace(/\s*\(.*\)$/, ""), "error", { ms: 7000 });
  else if (s === 413 || s === 422 || s === 400) toast(`${context ? context + ": " : ""}${msg}`, "warn", { ms: 7000 });
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

/* ---------------- markdown + math + citations ---------------- */
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
  if (window.marked && window.DOMPurify) {
    html = inline ? window.marked.parseInline(md, { gfm: true }) : window.marked.parse(md, { gfm: true, breaks: false });
  } else {
    html = escapeHtml(md).replace(/\n/g, "<br>");
  }
  html = html.replace(/MATHPH(\d+)XEND/g, (_, i) => {
    const { tex, display } = math[+i];
    if (window.katex) {
      try { return window.katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: "ignore", trust: false, output: "htmlAndMathml" }); }
      catch { /* fall through */ }
    }
    return escapeHtml(display ? `$$${tex}$$` : `$${tex}$`);
  });
  if (window.DOMPurify) {
    return window.DOMPurify.sanitize(html, { ADD_ATTR: ["target"], FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe"] });
  }
  return html.replace(/<(?!\/?(br|span|math|annotation|semantics|mrow|mi|mo|mn|msup|msub|mfrac|svg|path)\b)[^>]*>/gi, "");
}
const CITE_RE = /\[(?:(?:source|src)\s*)?(\d{1,2}(?:\s*[,–-]\s*\d{1,2})*)\]/gi;
function expandCites(s) {
  const out = [];
  for (const part of s.split(/\s*,\s*/)) {
    const m = part.match(/^(\d+)\s*[–-]\s*(\d+)$/);
    if (m) { for (let i = +m[1]; i <= Math.min(+m[2], +m[1] + 12); i++) out.push(i); }
    else if (/^\d+$/.test(part)) out.push(+part);
  }
  return out;
}
function linkCitations(root, sources) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest("code, pre, a, .katex, .cite") ? NodeFilter.FILTER_REJECT : CITE_RE.test(n.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const frag = document.createDocumentFragment();
    let last = 0;
    const text = node.nodeValue;
    text.replace(CITE_RE, (m, nums, offset) => {
      frag.append(text.slice(last, offset));
      for (const n of expandCites(nums)) frag.append(citeChip(n, sources));
      last = offset + m.length;
      return m;
    });
    frag.append(text.slice(last));
    node.replaceWith(frag);
  }
}
function citeChip(n, sources) {
  const src = (sources || []).find((s) => s.n === n);
  if (!src) return el("span", { class: "cite dead", title: "Source not available" }, String(n));
  return el("button", { class: "cite", type: "button", title: `${src.filename} · page ${src.page}`, "aria-label": `Source ${n}: ${src.filename}, page ${src.page}`, onclick: () => openSource(src) }, String(n));
}
function renderMd(md, sources, { inline = false, tag = "div", cls = "md" } = {}) {
  const box = el(tag, { class: cls });
  box.innerHTML = mdToSafeHtml(md, inline);
  for (const a of $$("a", box)) { a.target = "_blank"; a.rel = "noopener noreferrer"; }
  for (const t of $$("table", box)) { const w = el("div", { class: "table-wrap" }); t.replaceWith(w); w.append(t); }
  linkCitations(box, sources);
  return box;
}
const stripCites = (s) => String(s || "").replace(CITE_RE, "").replace(/\s+([.,;:])/g, "$1").trim();

/* ---------------- citation formatting (mirrors app/tools.py, re-run when the user edits fields) ---------------- */
const initials = (g) => g.split(/[\s.]+/).filter(Boolean).map((p) => `${p[0]}.`).join(" ");
function formatApa(c) {
  let names = c.authors.map((a) => `${a.family}, ${initials(a.given || "")}`.replace(/^, |, $/g, ""));
  if (names.length > 20) names = [...names.slice(0, 19), "…", names[names.length - 1]];
  const authors = names.length === 1 ? names[0] : names.length ? `${names.slice(0, -1).join(", ")}, & ${names[names.length - 1]}` : "";
  const year = `(${c.year || "n.d."}).`;
  const head = authors ? `${authors} ${year}` : `${c.title}. ${year}`;
  const title = authors && c.title ? ` ${c.title}.` : "";
  let src = c.venue || "";
  if (src && c.volume) src += `, ${c.volume}${c.issue ? `(${c.issue})` : ""}`;
  if (src && c.pages) src += `, ${c.pages}`;
  if (!src && c.publisher) src = c.publisher;
  const link = c.doi ? ` https://doi.org/${c.doi}` : c.url ? ` ${c.url}` : "";
  return `${head}${title}${src ? ` ${src}.` : ""}${link}`.replace(/\s+/g, " ").trim();
}
function formatMla(c) {
  const a = c.authors;
  const nm = (x) => `${x.family}, ${x.given}`.replace(/^, |, $/g, "");
  const authors = !a.length ? "" : a.length === 1 ? nm(a[0]) : a.length === 2 ? `${nm(a[0])}, and ${a[1].given} ${a[1].family}`.trim() : `${nm(a[0])}, et al`;
  const parts = [authors ? `${authors}.` : "", c.title ? `\u201c${c.title}.\u201d` : ""];
  const cont = [];
  if (c.venue) cont.push(c.venue);
  if (c.volume) cont.push(`vol. ${c.volume}`);
  if (c.issue) cont.push(`no. ${c.issue}`);
  if (c.publisher && !c.venue) cont.push(c.publisher);
  if (c.year) cont.push(c.year);
  if (c.pages) cont.push(`pp. ${c.pages}`);
  if (c.doi) cont.push(`https://doi.org/${c.doi}`); else if (c.url) cont.push(c.url);
  if (cont.length) parts.push(cont.join(", ") + ".");
  return parts.filter(Boolean).join(" ").replace(/\.\./g, ".").trim();
}
function formatBibtex(c) {
  const fam = ((c.authors[0]?.family || "anon").toLowerCase().replace(/[^a-z]/g, "")) || "anon";
  const word = (c.title.toLowerCase().match(/[a-z]+/g) || []).find((w) => w.length > 3) || "doc";
  const entry = { inproceedings: "inproceedings", book: "book", report: "techreport", thesis: "phdthesis", article: "article" }[c.type] || "misc";
  const clean = (v) => String(v || "").replace(/[{}]/g, "");
  const fields = [
    ["author", c.authors.map((x) => `${clean(x.family)}, ${clean(x.given)}`.replace(/^, |, $/g, "")).join(" and ")],
    ["title", c.title ? `{${clean(c.title)}}` : ""],
    [entry === "inproceedings" ? "booktitle" : "journal", ["article", "inproceedings"].includes(entry) ? clean(c.venue) : ""],
    ["publisher", clean(c.publisher)], ["year", c.year], ["volume", c.volume], ["number", c.issue],
    ["pages", (c.pages || "").replace(/-+/g, "--")], ["doi", c.doi], ["url", c.url],
  ];
  return `@${entry}{${fam}${c.year || "nd"}${word},\n${fields.filter(([, v]) => v).map(([k, v]) => `  ${k} = {${v}}`).join(",\n")}\n}`;
}

/* ---------------- sidebar: conversations ---------------- */
function groupLabel(ts) {
  const d = new Date(ts), now = new Date();
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = (day(now) - day(d)) / 86400000;
  if (diff < 1) return "Today";
  if (diff < 2) return "Yesterday";
  if (diff < 7) return "Previous 7 days";
  if (diff < 30) return "Previous 30 days";
  return "Older";
}
function renderConvs() {
  const list = $("#conv-list");
  list.replaceChildren();
  const q = state.search.trim().toLowerCase();
  const convs = state.convs
    .filter((c) => !q || c.title.toLowerCase().includes(q) || c.messages.some((m) => (m.content || "").toLowerCase().includes(q)))
    .sort((a, b) => b.updatedAt - a.updatedAt);
  if (!convs.length) {
    list.append(el("div", { class: "conv-empty" }, q ? "No chats match your search." : "Your chats will appear here."));
    return;
  }
  let group = "";
  for (const c of convs) {
    const g = groupLabel(c.updatedAt);
    if (g !== group) { group = g; list.append(el("div", { class: "conv-group" }, g)); }
    const item = el("div", { class: `conv${state.current?.id === c.id ? " active" : ""}`, role: "button", tabindex: "0", "aria-current": state.current?.id === c.id ? "page" : null,
      onclick: (e) => { if (!e.target.closest(".icon-btn")) openConv(c.id); },
      onkeydown: (e) => { if (e.key === "Enter" && e.target === item) openConv(c.id); } },
    el("span", { class: "conv-name", title: c.title }, c.title),
    el("span", { class: "conv-acts" },
      el("button", { class: "icon-btn", title: "Rename", "aria-label": `Rename ${c.title}`, onclick: () => renameConv(c) }, icon("edit")),
      el("button", { class: "icon-btn", title: "Delete", "aria-label": `Delete ${c.title}`, onclick: () => deleteConv(c) }, icon("trash"))));
    list.append(item);
  }
}
async function openConv(id) {
  if (state.busy) return toast("Wait for the current answer to finish.", "warn");
  const conv = state.convs.find((c) => c.id === id);
  if (!conv) return;
  setMode("chat");
  state.current = conv;
  renderConvs(); renderThread(); closeSidebarMobile();
  localStorage.setItem("dc-last", id);
}
function startNewChat() {
  if (state.busy) return;
  setMode("chat");
  state.current = newConv();
  renderConvs(); renderThread(); closeSidebarMobile();
  $("#input").focus();
}
async function renameConv(c) {
  const name = await promptDialog({ title: "Rename chat", value: c.title, ok: "Save" });
  if (name == null || !name.trim()) return;
  c.title = name.trim().slice(0, 120);
  c.renamed = true;
  await safeIdb(() => idb.put("convs", JSON.parse(JSON.stringify(c))));
  pushChat(c);
  renderConvs(); if (state.current?.id === c.id) $("#conv-title").textContent = c.title;
}
async function deleteConv(c) {
  const where = window.dcAuth?.user ? "this browser and your account" : "this browser";
  const ok = await promptDialog({ title: "Delete chat?", text: `"${c.title}" will be removed from ${where}.`, ok: "Delete", input: false });
  if (ok == null) return;
  state.convs = state.convs.filter((x) => x.id !== c.id);
  await safeIdb(() => idb.del("convs", c.id));
  window.dcAuth?.db.deleteChat(c.id);
  if (state.current?.id === c.id) startNewChat(); else renderConvs();
  toast("Chat deleted.", "ok");
}

/* ---------------- sidebar: documents ---------------- */
function renderDocs() {
  const list = $("#doc-list");
  list.replaceChildren();
  for (const d of state.docs) {
    const sel = state.selected.has(d.id);
    const meta = [plural(d.pages, "page")];
    const warn = d.status === "stale" ? "Re-upload needed" : d.status === "syncing" ? "Syncing…" : d.status === "serverOnly" ? "Viewer unavailable" : null;
    list.append(el("li", { class: "doc" },
      el("input", { type: "checkbox", checked: sel, disabled: d.status === "stale", "aria-label": `Include ${d.filename} in questions`, title: "Include in questions and tools",
        onchange: (e) => { e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id); saveSelection(); renderScope(); } }),
      el("span", { class: "doc-ico" }, "PDF"),
      el("div", { class: "doc-main", title: `Open ${d.filename}`, onclick: () => openDocViewer(d.id, 1) },
        el("div", { class: "doc-name" }, d.filename),
        el("div", { class: "doc-meta" }, meta.join(" · "), warn ? el("span", { class: "warn" }, `· ${warn}`) : null)),
      el("button", { class: "icon-btn", title: "Remove document", "aria-label": `Remove ${d.filename}`, onclick: () => removeDoc(d) }, icon("trash"))));
  }
  for (const u of state.uploads) {
    list.append(el("li", { class: "doc uploading" },
      el("span", { class: "doc-ico" }, "PDF"),
      el("div", { class: "doc-main" },
        el("div", { class: "doc-name" }, u.name),
        el("div", { class: "doc-meta" }, u.phase === "upload" ? `Uploading ${Math.round(u.progress * 100)}%` : u.phase === "queued" ? "Waiting…" : "Reading & indexing…"),
        el("div", { class: `progress${u.phase === "index" ? " indeterminate" : ""}` }, el("span", { style: `width:${Math.round(u.progress * 100)}%` })))));
  }
  const limit = state.config?.docs_limit ?? 3;
  $("#lib-count").textContent = `${state.docs.length}/${limit}`;
  $("#mini-drop").hidden = state.docs.length + state.uploads.length >= limit;
  renderScope();
}
async function removeDoc(d) {
  const ok = await promptDialog({ title: "Remove document?", text: `"${d.filename}" will be removed from your library. Chats that cite it keep their text.`, ok: "Remove", input: false });
  if (ok == null) return;
  try { await api(`/api/documents/${encodeURIComponent(d.id)}`, { method: "DELETE" }); }
  catch (e) { if (e.status !== 404) return handleError(e); }
  await safeIdb(() => idb.del("docs", d.id));
  state.docs = state.docs.filter((x) => x.id !== d.id);
  state.selected.delete(d.id); saveSelection();
  if (viewer.docId === d.id) closeViewer();
  renderDocs(); refreshUsage(); if (!state.current?.messages.length) renderThread();
  toast(`Removed ${d.filename}.`, "ok");
}
function renderUsage() {
  const u = state.usage; if (!u) return;
  $("#usage-q").textContent = `${u.questions_used} / ${u.questions_limit}`;
  const pct = clamp((100 * u.questions_used) / Math.max(1, u.questions_limit), 0, 100);
  $("#usage-bar").style.width = `${pct}%`;
  const m = $("#usage-meter");
  m.classList.toggle("high", pct >= 80);
  m.setAttribute("aria-valuenow", u.questions_used); m.setAttribute("aria-valuemax", u.questions_limit);
}
async function refreshUsage() { try { setUsage(await apiJson("/api/usage")); } catch { /* offline: keep last */ } }

/* ---------------- scope ---------------- */
function renderScope() {
  const ready = readyDocs();
  const scoped = scopeDocs();
  const label = !state.docs.length ? "No documents" : !scoped.length ? "No documents selected"
    : scoped.length === ready.length ? (ready.length === 1 ? ready[0].filename : `All documents (${ready.length})`)
    : scoped.length === 1 ? scoped[0].filename : `${scoped.length} of ${ready.length} documents`;
  $("#scope-label").textContent = label;
  for (const b of $$(".tool")) {
    const need = b.dataset.tool === "compare" ? 2 : 1;
    b.disabled = scoped.length < need || state.busy;
    b.title = scoped.length < need ? (need === 2 ? "Select at least two documents to compare" : "Upload or select a document first") : "";
  }
  updateSend();
}
function toggleScopePop(show) {
  const pop = $("#scope-pop");
  if (show === false || (!pop.hidden && show !== true)) { pop.hidden = true; return; }
  pop.replaceChildren(el("h3", {}, "Ask these documents"));
  if (!state.docs.length) pop.append(el("div", { class: "pop-item muted" }, "Upload a PDF to get started."));
  for (const d of state.docs) {
    pop.append(el("label", { class: "pop-item" },
      el("input", { type: "checkbox", checked: state.selected.has(d.id), disabled: d.status === "stale",
        onchange: (e) => { e.target.checked ? state.selected.add(d.id) : state.selected.delete(d.id); saveSelection(); renderDocs(); } }),
      el("span", {}, d.filename), el("small", {}, `${d.pages}p`)));
  }
  if (state.docs.length > 1) {
    pop.append(el("div", { class: "pop-foot" },
      el("button", { onclick: () => { readyDocs().forEach((d) => state.selected.add(d.id)); saveSelection(); renderDocs(); toggleScopePop(true); } }, "Select all"),
      el("button", { onclick: () => { state.selected.clear(); saveSelection(); renderDocs(); toggleScopePop(true); } }, "Clear")));
  }
  pop.hidden = false;
  const r = $("#scope-btn").getBoundingClientRect();
  pop.style.top = `${r.bottom + 6}px`;
  pop.style.left = `${clamp(r.right - 300, 8, window.innerWidth - 308)}px`;
}

/* ---------------- thread ---------------- */
const TOOL_META = {
  summarize: { icon: "tldr", label: "Summary", verb: "Summarize" },
  explain: { icon: "bulb", label: "Explained simply", verb: "Explain simply" },
  flashcards: { icon: "cards", label: "Flashcards", verb: "Make flashcards from" },
  quiz: { icon: "quiz", label: "Quiz", verb: "Quiz me on" },
  brief: { icon: "flask", label: "Research brief", verb: "Write a research brief of" },
  compare: { icon: "columns", label: "Comparison", verb: "Compare" },
  cite: { icon: "quote", label: "Citation", verb: "Cite" },
};
function renderThread() {
  const thread = $("#thread");
  thread.replaceChildren();
  const conv = state.current;
  if (mode === "chat") {
    $("#conv-title").textContent = conv?.messages.length ? conv.title : "New chat";
    document.title = conv?.messages.length ? `${conv.title} · DocuChat` : "DocuChat";
  }
  if (!conv?.messages.length) { thread.append(renderEmpty()); updateSend(); return; }
  const col = el("div", { class: "col" });
  conv.messages.forEach((m, i) => col.append(renderMessage(m, i === conv.messages.length - 1)));
  thread.append(col);
  requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  updateSend();
}
function renderEmpty() {
  const col = el("div", { class: "col empty" });
  if (!state.docs.length && !state.uploads.length) {
    col.append(
      el("div", { class: "empty-art" }, icon("file")),
      el("h2", {}, "Study smarter from your own notes"),
      el("p", {}, "Upload lecture notes, a textbook chapter or research papers. Ask anything with answers cited to the page, or switch to Exam Prep to practise real exam questions."),
      el("label", { class: "big-drop", tabindex: "0", onclick: (e) => { e.preventDefault(); $("#file-input").click(); }, onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file-input").click(); } } },
        icon("upload"), el("b", {}, "Drop PDFs here or click to browse"),
        el("small", {}, `Up to ${state.config?.docs_limit ?? 3} PDFs · ${state.config?.max_upload_mb ?? 20} MB each · text-based PDFs`)),
      el("div", { class: "feature-row" },
        el("div", {}, el("b", {}, "Cited answers"), "Click any [1] to open the page."),
        el("button", { class: "feature-link", type: "button", onclick: () => setMode("exam", { history: "push" }) }, el("b", {}, "Exam Prep →"), "University, WAEC, JAMB. AI-marked."),
        el("div", {}, el("b", {}, "Study tools"), "Summaries, flashcards, quizzes.")));
    return col;
  }
  const scoped = scopeDocs();
  const name = scoped.length === 1 ? scoped[0].filename : scoped.length ? `${scoped.length} documents` : "your documents";
  col.append(
    el("div", { class: "empty-art" }, icon("sparkle")),
    el("h2", {}, state.uploads.length && !state.docs.length ? "Indexing your PDF…" : `What do you want to know?`),
    el("p", {}, state.uploads.length && !state.docs.length ? "This takes a few seconds per document." : `Asking ${name}. Pick a starter or type your own question.`));
  if (state.docs.length) {
    const sug = [
      { icon: "tldr", label: "Summarize this", sub: "TL;DR and key points", tool: "summarize" },
      { icon: "chat", label: "What is the main argument or finding?", sub: "Cited answer", ask: "What is the main argument or finding, and what evidence supports it?" },
      { icon: "bulb", label: "Explain the core idea simply", sub: "Plain language, key terms", tool: "explain" },
      { icon: "cards", label: "Make flashcards", sub: "Flip cards · Anki export", tool: "flashcards" },
      { icon: "quiz", label: "Quiz me", sub: "8 questions with explanations", tool: "quiz" },
      scoped.length >= 2
        ? { icon: "columns", label: "Compare these documents", sub: "Side-by-side table", tool: "compare" }
        : { icon: "flask", label: "Extract the methodology", sub: "Research brief", tool: "brief" },
      { icon: "grad", label: "Practise exam questions", sub: "Exam Prep · MCQ & theory, AI-marked", exam: true },
    ];
    col.append(el("div", { class: "suggest-title" }, "Try"),
      el("div", { class: "suggests" }, sug.map((s) => el("button", { class: s.exam ? "suggest wide" : "suggest", onclick: () => (s.exam ? setMode("exam", { history: "push" }) : s.tool ? runTool(s.tool) : sendChat(s.ask)) },
        icon(s.icon), el("div", {}, s.label, el("small", {}, s.sub))))));
  }
  return col;
}
function renderMessage(m, isLast) {
  if (m.role === "user") {
    return el("div", { class: "msg user" }, el("div", { class: "bubble" }, m.content));
  }
  const body = el("div", { class: "bot-body" });
  const wrap = el("div", { class: `msg bot${isLast ? " last" : ""}`, dataset: { id: m.id } }, el("div", { class: "avatar" }, icon(m.kind === "tool" ? TOOL_META[m.tool]?.icon || "sparkle" : "sparkle")), body);
  fillBotBody(body, m, isLast);
  return wrap;
}
function fillBotBody(body, m, isLast) {
  body.replaceChildren();
  if (m.kind === "tool" && !m.error && m.data) {
    const meta = TOOL_META[m.tool] || { icon: "sparkle", label: m.tool };
    body.append(el("div", { class: "tool-head" }, el("span", { class: "tag" }, icon(meta.icon), meta.label), (m.docNames || []).join(", ")));
    try { body.append(renderTool(m)); }
    catch (e) { console.warn(e); body.append(renderMd(m.content, m.sources)); }
  } else if (m.pending && !m.content) {
    body.append(el("div", { class: "status-line" }, el("span", { class: "thinking", "aria-label": "Thinking" }, el("i"), el("i"), el("i")), m.status || ""));
  } else if (m.content) {
    const md = renderMd(m.content, m.sources);
    if (m.pending) md.classList.add("cursor");
    body.append(md);
  }
  if (m.error) {
    body.append(el("div", { class: "err-box" }, el("span", {}, m.error), isLast ? el("button", { class: "btn", onclick: () => regenerate() }, icon("refresh"), "Try again") : null));
  }
  if (!m.pending && m.sources?.length && m.kind !== "tool") body.append(renderSources(m.sources));
  if (m.kind === "tool" && !m.pending && m.sources?.length && !["cite"].includes(m.tool)) body.append(renderSources(m.sources));
  if (!m.pending && !m.error) {
    const acts = el("div", { class: "msg-actions" },
      el("button", { class: "act", title: "Copy as Markdown", onclick: (e) => copyText(m.content, e.currentTarget) }, icon("copy"), el("span", {}, "Copy")),
      isLast ? el("button", { class: "act", title: "Regenerate", onclick: () => regenerate() }, icon("refresh"), el("span", {}, "Regenerate")) : null);
    if (m.tool === "flashcards") acts.append(el("button", { class: "act", onclick: () => exportAnki(m) }, icon("download"), el("span", {}, "Export CSV (Anki)")));
    body.append(acts);
  }
}
function renderSources(sources) {
  const seen = new Set();
  return el("div", { class: "sources", "aria-label": "Sources" }, sources.filter((s) => !seen.has(s.n) && seen.add(s.n)).slice(0, 16).map((s) =>
    el("button", { class: "src", title: s.snippet || "", onclick: () => openSource(s) }, el("b", {}, String(s.n)), el("span", {}, s.filename), el("em", {}, `p.${s.page}`))));
}

/* ---------------- tool widgets ---------------- */
function renderTool(m) {
  const d = m.data, src = m.sources;
  const inline = (t) => renderMd(t, src, { inline: true, tag: "span", cls: "md" });
  const block = (t) => renderMd(t, src);
  const na = (t) => (!t || /^not stated/i.test(t) ? el("span", { class: "na" }, "Not stated in the document.") : inline(t));
  const list = (items) => items?.length ? el("ul", { class: "md" }, items.map((x) => el("li", {}, na(x)))) : el("p", { class: "na" }, "Not stated in the document.");
  switch (m.tool) {
    case "summarize":
      return el("div", {},
        el("div", { class: "callout" }, el("div", { class: "callout-label" }, "TL;DR"), block(d.tldr)),
        el("div", { class: "section-label" }, "Key points"), list(d.key_points),
        d.takeaway ? el("div", {}, el("div", { class: "section-label" }, "Takeaway"), block(d.takeaway)) : null);
    case "explain":
      return el("div", {},
        d.title ? el("h3", { class: "md", style: "margin:0 0 8px;font-size:17px" }, stripCites(d.title)) : null,
        block(d.explanation),
        d.analogy ? el("div", { class: "callout", style: "margin-top:12px" }, el("div", { class: "callout-label" }, "Analogy"), block(d.analogy)) : null,
        d.key_terms?.length ? el("div", {}, el("div", { class: "section-label" }, "Key terms"),
          el("div", { class: "terms" }, d.key_terms.map((t) => el("div", { class: "term" }, el("b", {}, stripCites(t.term)), inline(t.meaning))))) : null,
        d.check_question ? el("div", {}, el("div", { class: "section-label" }, "Check yourself"), block(d.check_question)) : null);
    case "flashcards": return renderDeck(m);
    case "quiz": return renderQuiz(m);
    case "brief":
      return el("div", { class: "brief" }, d.items.map((it) => {
        const s2 = it.sources || src;
        const inl = (t) => (!t || /^not stated/i.test(t) ? el("span", { class: "na" }, "Not stated in the document.") : renderMd(t, s2, { inline: true, tag: "span" }));
        const ls = (items) => items?.length ? el("ul", { class: "md" }, items.map((x) => el("li", {}, inl(x)))) : el("p", { class: "na", style: "margin:0" }, "Not stated in the document.");
        return el("div", { style: "margin-bottom:14px" },
          el("h3", {}, it.title || it.filename),
          el("div", { class: "brief-grid" },
            el("div", { class: "brief-card wide" }, el("div", { class: "section-label" }, "Research question"), inl(it.research_question)),
            el("div", { class: "brief-card wide" }, el("div", { class: "section-label" }, "Methodology"), inl(it.methodology)),
            el("div", { class: "brief-card wide" }, el("div", { class: "section-label" }, "Key findings"), ls(it.key_findings)),
            el("div", { class: "brief-card" }, el("div", { class: "section-label" }, "Limitations"), ls(it.limitations)),
            el("div", { class: "brief-card" }, el("div", { class: "section-label" }, "Future work"), ls(it.future_work)),
            it.contribution ? el("div", { class: "brief-card wide" }, el("div", { class: "section-label" }, "Main contribution"), inl(it.contribution)) : null),
          d.items.length > 1 ? renderSources(s2) : null);
      }));
    case "compare":
      return el("div", {},
        el("div", { class: "table-wrap md" }, el("table", {},
          el("thead", {}, el("tr", {}, el("th", {}, "Aspect"), d.documents.map((n) => el("th", {}, n)))),
          el("tbody", {}, d.rows.map((r) => el("tr", {}, el("td", {}, el("b", {}, stripCites(r.aspect))), r.cells.map((c) => el("td", {}, na(c)))))))),
        d.summary ? el("div", {}, el("div", { class: "section-label" }, "Summary"), block(d.summary)) : null);
    case "cite": return el("div", {}, d.items.map((it) => renderCiteCard(m, it)));
    default: return block(m.content);
  }
}
function renderDeck(m) {
  const cards = m.data.cards;
  m.ui ||= { i: 0, list: false };
  const deck = el("div", { class: "deck", tabindex: "0", "aria-label": "Flashcards. Space to flip, arrow keys to move." });
  const draw = () => {
    const i = clamp(m.ui.i, 0, cards.length - 1);
    deck.replaceChildren();
    deck.append(el("div", { class: "deck-bar" },
      el("span", {}, m.ui.list ? plural(cards.length, "card") : `Card ${i + 1} of ${cards.length}`),
      el("div", { class: "acts" },
        el("button", { class: "act", onclick: () => { m.ui.list = !m.ui.list; draw(); } }, icon(m.ui.list ? "cards" : "tldr"), el("span", {}, m.ui.list ? "Study mode" : "Show all")),
        el("button", { class: "act", onclick: () => { for (let k = cards.length - 1; k > 0; k--) { const j = Math.floor(Math.random() * (k + 1)); [cards[k], cards[j]] = [cards[j], cards[k]]; } m.ui.i = 0; draw(); saveConv(); } }, icon("shuffle"), el("span", {}, "Shuffle")),
        el("button", { class: "act", onclick: () => exportAnki(m) }, icon("download"), el("span", {}, "Anki CSV")))));
    if (m.ui.list) {
      deck.append(el("div", { class: "deck-list" }, cards.map((c) => el("div", { class: "row" },
        el("b", {}, renderMd(c.front, m.sources, { inline: true, tag: "span" })), renderMd(c.back, m.sources, { inline: true, tag: "span" })))));
      return;
    }
    const card = el("div", { class: "flip-card", role: "button", tabindex: "-1", "aria-label": "Flip card", onclick: (e) => { if (!e.target.closest(".cite")) card.classList.toggle("flipped"); } },
      el("div", { class: "face front" }, el("span", { class: "face-label" }, "Question"), renderMd(cards[i].front, m.sources)),
      el("div", { class: "face back" }, el("span", { class: "face-label" }, "Answer"), renderMd(cards[i].back, m.sources)));
    deck.append(el("div", { class: "flip-wrap" }, card),
      el("div", { class: "deck-nav" },
        el("button", { class: "icon-btn", "aria-label": "Previous card", disabled: i === 0, onclick: () => { m.ui.i = i - 1; draw(); } }, icon("left")),
        cards.length <= 40 ? el("div", { class: "deck-dots" }, cards.map((_, k) => el("i", { class: k === i ? "on" : "" }))) : el("span", {}, `${i + 1}/${cards.length}`),
        el("button", { class: "icon-btn", "aria-label": "Next card", disabled: i === cards.length - 1, onclick: () => { m.ui.i = i + 1; draw(); } }, icon("right"))),
      el("p", { class: "composer-hint", style: "margin-top:6px" }, "Click the card or press Space to flip · ← → to move"));
  };
  deck.addEventListener("keydown", (e) => {
    if (m.ui.list) return;
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); $(".flip-card", deck)?.classList.toggle("flipped"); }
    else if (e.key === "ArrowRight" && m.ui.i < cards.length - 1) { m.ui.i++; draw(); }
    else if (e.key === "ArrowLeft" && m.ui.i > 0) { m.ui.i--; draw(); }
  });
  draw();
  return deck;
}
function exportAnki(m) {
  const pageOf = (n) => m.sources?.find((s) => s.n === n);
  const fix = (t) => String(t).replace(CITE_RE, (_, nums) => {
    const ps = expandCites(nums).map(pageOf).filter(Boolean).map((s) => `p. ${s.page}`);
    return ps.length ? ` (${[...new Set(ps)].join(", ")})` : "";
  }).replace(/\s+/g, " ").trim();
  const q = (t) => `"${fix(t).replace(/"/g, '""')}"`;
  const csv = "#separator:comma\n#html:false\n#columns:Front,Back\n" + m.data.cards.map((c) => `${q(c.front)},${q(c.back)}`).join("\n") + "\n";
  const base = (m.docNames?.[0] || "docuchat").replace(/\.pdf$/i, "").replace(/[^\w.-]+/g, "-").slice(0, 60);
  download(`${base}-flashcards.csv`, csv, "text/csv;charset=utf-8");
  toast("Flashcards exported. In Anki: File → Import.", "ok");
}
function renderQuiz(m) {
  const qs = m.data.questions;
  m.answers ||= {};
  const root = el("div", { class: "quiz" });
  const draw = () => {
    const answered = Object.keys(m.answers).length;
    const correct = qs.filter((q, i) => m.answers[i] === q.answer).length;
    root.replaceChildren(el("div", { class: "quiz-score" },
      el("span", {}, answered ? el("b", {}, `${correct} / ${answered} correct`) : `${plural(qs.length, "question")} · pick an answer to check it`),
      answered ? el("button", { class: "act", onclick: () => { m.answers = {}; draw(); saveConv(); } }, icon("refresh"), el("span", {}, "Retry")) : null));
    qs.forEach((q, i) => {
      const chosen = m.answers[i];
      const done = chosen !== undefined;
      root.append(el("div", { class: "q" },
        el("div", { class: "q-text" }, el("span", { class: "n" }, `${i + 1}.`), renderMd(q.question, m.sources, { inline: true, tag: "span" })),
        el("div", { class: "opts", role: "group" }, q.options.map((o, j) => el("button", {
          class: `opt${done && j === q.answer ? " correct" : ""}${done && j === chosen && j !== q.answer ? " wrong" : ""}`,
          disabled: done, "aria-pressed": done && j === chosen ? "true" : null,
          onclick: () => { m.answers[i] = j; draw(); saveConv(); },
        }, el("span", { class: "letter" }, "ABCDEF"[j]), renderMd(o, m.sources, { inline: true, tag: "span" })))),
        done ? el("div", { class: "explain" }, el("b", {}, chosen === q.answer ? "Correct. " : `Answer: ${"ABCDEF"[q.answer]}. `), renderMd(q.explanation || "", m.sources, { inline: true, tag: "span" })) : null));
    });
  };
  draw();
  return root;
}
function renderCiteCard(m, it) {
  const card = el("div", { class: "cite-card" });
  const fmtBox = el("div", { class: "formats" });
  const drawFormats = () => {
    const f = { apa: formatApa(it), mla: formatMla(it), bibtex: formatBibtex(it) };
    it.formatted = f;
    fmtBox.replaceChildren(...[["APA 7", "apa"], ["MLA 9", "mla"], ["BibTeX", "bibtex"]].map(([label, k]) =>
      el("div", { class: "fmt" },
        el("div", { class: "fmt-label" }, label, el("button", { class: "act", onclick: (e) => copyText(f[k], e.currentTarget) }, icon("copy"), el("span", {}, "Copy"))),
        el("div", { class: `fmt-text${k === "bibtex" ? " mono" : ""}` }, f[k]))));
  };
  const save = debounce(() => { m.content = citeMarkdown(m.data.items); saveConv(); }, 400);
  const input = (k, label, wide = false) => {
    const inp = k === "type"
      ? el("select", {}, ["article", "inproceedings", "book", "report", "thesis", "misc"].map((t) => el("option", { value: t, selected: it[k] === t }, t)))
      : el("input", { type: "text", value: it[k] || "" });
    inp.addEventListener("input", () => { it[k] = inp.value.trim(); drawFormats(); save(); });
    return el("label", { class: wide ? "wide" : "" }, label, inp);
  };
  const authors = el("textarea", { rows: "3", placeholder: "Family, Given (one per line)" });
  authors.value = it.authors.map((a) => [a.family, a.given].filter(Boolean).join(", ")).join("\n");
  authors.addEventListener("input", () => {
    it.authors = authors.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
      const [family, ...rest] = l.split(",");
      return rest.length ? { family: family.trim(), given: rest.join(",").trim() } : (() => { const p = l.split(/\s+/); return { family: p.pop(), given: p.join(" ") }; })();
    });
    drawFormats(); save();
  });
  card.append(
    el("h4", {}, icon("file"), it.filename || it.title),
    fmtBox,
    el("details", { class: "cite-edit" }, el("summary", {}, "Edit details"),
      el("div", { class: "cite-form" },
        input("title", "Title", true),
        el("label", { class: "wide" }, "Authors (Family, Given · one per line)", authors),
        input("year", "Year"), input("type", "Type"), input("venue", "Journal / conference", true),
        input("volume", "Volume"), input("issue", "Issue"), input("pages", "Pages"), input("publisher", "Publisher"),
        input("doi", "DOI"), input("url", "URL"))),
    el("p", { class: "composer-hint", style: "text-align:left" }, "Built from the title page and PDF metadata. Check details before you submit."));
  drawFormats();
  return card;
}
const citeMarkdown = (items) => items.map((it) => `**APA:** ${it.formatted.apa}\n\n**MLA:** ${it.formatted.mla}\n\n\`\`\`bibtex\n${it.formatted.bibtex}\n\`\`\``).join("\n\n---\n\n");

/* ---------------- chat ---------------- */
function historyFor(messages) {
  return messages.filter((m) => !m.error && !m.pending && m.content)
    .slice(-HISTORY_TURNS)
    .map((m) => ({ role: m.role, content: m.content.length > HISTORY_CHARS ? m.content.slice(0, HISTORY_CHARS) + " …" : m.content }));
}
function ensureCanAsk(needDocs = 1) {
  if (state.busy) return false;
  if (!state.docs.length) { toast("Upload a PDF first, then ask away.", "warn"); return false; }
  if (scopeDocs().length < needDocs) { toast(needDocs > 1 ? "Select at least two documents to compare." : "Select at least one document in your library.", "warn"); return false; }
  return true;
}
function setBusy(b) {
  state.busy = b;
  const send = $("#send");
  send.classList.toggle("stop", b);
  send.replaceChildren(icon(b ? "stop" : "send"));
  send.setAttribute("aria-label", b ? "Stop" : "Send");
  send.title = b ? "Stop" : "Send (Enter)";
  renderScope();
}
function updateSend() { $("#send").disabled = !state.busy && !$("#input").value.trim(); }
function titleFrom(text) { const t = text.replace(/\s+/g, " ").trim(); return t.length > 60 ? t.slice(0, 57) + "…" : t || "New chat"; }
function liveUpdate(m) {
  const node = $(`.msg.bot[data-id="${m.id}"] .bot-body`);
  if (node) fillBotBody(node, m, true);
  const thread = $("#thread");
  if (thread.scrollHeight - thread.scrollTop - thread.clientHeight < 240) thread.scrollTop = thread.scrollHeight;
}
async function sendChat(question, { regenerateOf = null } = {}) {
  question = (question || "").trim();
  if (!question || !ensureCanAsk()) return;
  const conv = state.current;
  const prior = historyFor(conv.messages);
  if (!regenerateOf) {
    conv.messages.push({ id: uid(), role: "user", kind: "chat", content: question, ts: Date.now() });
    if (!conv.renamed && conv.messages.filter((x) => x.role === "user").length === 1) conv.title = titleFrom(question);
  }
  const bot = { id: uid(), role: "assistant", kind: "chat", content: "", sources: [], pending: true, ts: Date.now(), status: "Searching your documents…" };
  conv.messages.push(bot);
  renderThread();
  setBusy(true);
  const ctrl = new AbortController(); state.abort = ctrl;
  try {
    await ensureSynced();
    const res = await api("/api/chat", { method: "POST", signal: ctrl.signal,
      body: { question, doc_ids: scopeDocs().map((d) => d.id), history: regenerateOf ? prior.slice(0, -1) : prior } });
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = ""; let raf = 0;
    const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; liveUpdate(bot); }); };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        let ev; try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "sources") { bot.sources = ev.sources; bot.status = ev.sources.length ? `Reading ${plural(ev.sources.length, "passage")}…` : "Checking…"; }
        else if (ev.type === "token") bot.content += ev.text;
        else if (ev.type === "error") bot.error = ev.message.replace(/\s*\(.*\)$/, "");
        else if (ev.type === "done") setUsage(ev.usage);
        schedule();
      }
    }
    bot.content = bot.content.trim();
    if (!bot.content && !bot.error) bot.error = "No answer came back. Please try again.";
  } catch (e) {
    if (e.name === "AbortError") { bot.content = bot.content.trim() || "_Stopped._"; }
    else { bot.error = e.message?.replace(/\s*\(.*\)$/, "") || "Something went wrong."; handleError(e); }
  } finally {
    bot.pending = false; delete bot.status;
    state.abort = null; setBusy(false);
    renderThread(); await saveConv(conv);
  }
}
async function runTool(tool, opts = {}) {
  if (!ensureCanAsk(tool === "compare" ? 2 : 1)) return;
  const docs = scopeDocs();
  const meta = TOOL_META[tool];
  const typed = $("#input").value.trim();
  const topic = opts.topic ?? (["explain", "flashcards", "quiz", "compare"].includes(tool) && typed ? typed.slice(0, 500) : undefined);
  if (topic && opts.topic === undefined) { $("#input").value = ""; autosize(); }
  const conv = state.current;
  const names = docs.map((d) => d.filename);
  const label = `${meta.verb} ${names.length === 1 ? names[0] : names.length === 2 ? names.join(" and ") : `${names.length} documents`}${topic ? (tool === "compare" ? ` on: ${topic}` : ` · focus: ${topic}`) : ""}`;
  if (!opts.regenerate) {
    conv.messages.push({ id: uid(), role: "user", kind: "tool", tool, content: label, opts: { topic, docIds: docs.map((d) => d.id) }, ts: Date.now() });
    if (!conv.renamed && conv.messages.filter((x) => x.role === "user").length === 1) conv.title = titleFrom(`${meta.label}: ${names[0]}`);
  }
  const docIds = opts.docIds || docs.map((d) => d.id);
  const bot = { id: uid(), role: "assistant", kind: "tool", tool, content: "", pending: true, ts: Date.now(), docNames: docIds.map((id) => docById(id)?.filename || id),
    status: { summarize: "Reading the whole document…", explain: "Finding the core ideas…", flashcards: "Writing flashcards…", quiz: "Writing questions…", brief: "Extracting methods and findings…", compare: "Lining up the documents…", cite: "Reading the title page…" }[tool] };
  conv.messages.push(bot);
  renderThread(); setBusy(true);
  const ctrl = new AbortController(); state.abort = ctrl;
  try {
    await ensureSynced();
    const body = { doc_ids: docIds };
    if (topic) body.topic = topic;
    const r = await apiJson(`/api/tools/${tool}`, { method: "POST", body, signal: ctrl.signal });
    bot.data = r.data; bot.sources = r.sources?.length ? r.sources : r.data.items?.[0]?.sources || []; bot.content = r.markdown;
    setUsage(r.usage);
  } catch (e) {
    if (e.name === "AbortError") bot.error = "Stopped.";
    else { bot.error = e.message?.replace(/\s*\(.*\)$/, "") || "Something went wrong."; handleError(e); }
  } finally {
    bot.pending = false; delete bot.status;
    state.abort = null; setBusy(false);
    renderThread(); await saveConv(conv);
  }
}
async function regenerate() {
  if (state.busy) return;
  const msgs = state.current.messages;
  if (msgs[msgs.length - 1]?.role === "assistant") msgs.pop();
  const user = msgs[msgs.length - 1];
  if (!user || user.role !== "user") return renderThread();
  if (user.kind === "tool") {
    const ids = (user.opts?.docIds || []).filter((id) => readyDocs().some((d) => d.id === id));
    if (!ids.length) { toast("Those documents are no longer in your library.", "warn"); return renderThread(); }
    return runTool(user.tool, { topic: user.opts?.topic, docIds: ids, regenerate: true });
  }
  return sendChat(user.content, { regenerateOf: user });
}

/* ---------------- upload ---------------- */
function addFiles(fileList) {
  const files = [...fileList].filter((f) => /\.pdf$/i.test(f.name) || f.type === "application/pdf");
  if (!files.length) return toast("Only PDF files can be added.", "warn");
  const limit = state.config?.docs_limit ?? 3;
  const room = limit - state.docs.length - state.uploads.length;
  if (room <= 0) return toast(`The free plan holds ${limit} PDFs. Remove one to add another.`, "warn", { ms: 6000 });
  if (files.length > room) toast(`Only ${plural(room, "more PDF")} fit${room === 1 ? "s" : ""} on the free plan; adding the first ${room}.`, "warn", { ms: 6000 });
  const maxMb = state.config?.max_upload_mb ?? 20;
  for (const f of files.slice(0, room)) {
    if (f.size > maxMb * 1024 * 1024) { toast(`${f.name} is over ${maxMb} MB.`, "warn"); continue; }
    state.uploads.push({ tmp: uid(), name: f.name, file: f, progress: 0, phase: "queued" });
  }
  renderDocs(); if (!state.current.messages.length) renderThread();
  pumpUploads();
}
let pumping = false;
async function pumpUploads() {
  if (pumping) return; pumping = true;
  try {
    while (state.uploads.length) {
      const u = state.uploads[0];
      try { await uploadOne(u); }
      catch (e) { handleError(e, u.name); }
      state.uploads.shift();
      renderDocs();
    }
  } finally { pumping = false; if (!state.current.messages.length) renderThread(); }
}
function uploadOne(u) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const fd = new FormData(); fd.append("file", u.file, u.name);
    xhr.open("POST", "/api/documents");
    xhr.responseType = "json";
    u.phase = "upload"; renderDocs();
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) { u.progress = e.loaded / e.total; if (u.progress >= 1) u.phase = "index"; renderDocs(); } };
    xhr.upload.onload = () => { u.phase = "index"; u.progress = 1; renderDocs(); };
    xhr.onerror = () => reject(new ApiError(0, "Upload failed. Check your connection and try again."));
    xhr.onload = async () => {
      const body = xhr.response || {};
      if (xhr.status !== 201) {
        let detail = body.detail; if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
        return reject(new ApiError(xhr.status, detail || `Upload failed (${xhr.status})`, body));
      }
      const rec = { id: body.id, filename: body.filename, pages: body.pages, chunks: body.chunks, meta: body.meta || {}, size: u.file.size, addedAt: Date.now(), blob: u.file, index: body.index };
      await safeIdb(() => idb.put("docs", rec));
      state.docs.push({ id: rec.id, filename: rec.filename, pages: rec.pages, chunks: rec.chunks, meta: rec.meta, size: rec.size, addedAt: rec.addedAt, status: "ready" });
      state.selected.add(rec.id); saveSelection();
      setUsage(body.usage); state.lastOk = Date.now();
      toast(`${rec.filename} is ready · ${plural(rec.pages, "page")}`, "ok");
      resolve();
    };
    xhr.send(fd);
  });
}

/* ---------------- sync / restore ---------------- */
async function restoreDoc(id) {
  const d = docById(id);
  const rec = await safeIdb(() => idb.get("docs", id), null);
  if (!rec?.index) { if (d) d.status = "stale"; return false; }
  if (d) { d.status = "syncing"; renderDocs(); }
  try {
    const r = await apiJson("/api/documents/restore", { method: "POST", retry: false, body: {
      id: rec.id, filename: rec.filename, pages: rec.pages, meta: rec.meta || {},
      dim: rec.index.dim, dtype: rec.index.dtype || "float16", chunks: rec.index.chunks, vectors: rec.index.vectors } });
    if (d) d.status = "ready";
    setUsage(r.usage);
    return true;
  } catch (e) {
    if (d) d.status = e.status === 422 ? "stale" : "ready";
    if (e.status === 422) { state.selected.delete(id); toast(`${rec.filename} needs to be re-uploaded (the AI model changed).`, "warn", { ms: 8000 }); }
    else if (e.status === 402) toast(e.message, "warn", isGuest() ? { link: SIGNUP_LINK(), ms: 12000 } : {});
    else throw e;
    return false;
  } finally { renderDocs(); }
}
async function restoreMany(ids) { for (const id of ids) await restoreDoc(id); }
async function syncDocs({ silent = false } = {}) {
  let server;
  try { server = await apiJson("/api/documents"); }
  catch (e) { if (!silent) handleError(e); return; }
  const have = new Set(server.map((d) => d.id));
  for (const d of state.docs) if (!have.has(d.id) && d.status !== "stale") { try { await restoreDoc(d.id); } catch (e) { if (!silent) handleError(e); } }
  for (const s of server) if (!docById(s.id)) state.docs.push({ ...s, meta: {}, status: "serverOnly" });
  renderDocs();
}

/* ---------------- PDF viewer ---------------- */
const viewer = { docId: null, page: 1, pdf: null, pdfFor: null, zoom: 1, token: 0, lib: null, text: "" };
async function pdfLib() {
  if (viewer.lib) return viewer.lib;
  const lib = await import(`${PDFJS}/pdf.min.mjs`);
  lib.GlobalWorkerOptions.workerSrc = `${PDFJS}/pdf.worker.min.mjs`;
  return (viewer.lib = lib);
}
function showViewer() {
  $("#viewer").hidden = false;
  $("#shell").classList.add("viewer-open");
}
function closeViewer() {
  $("#viewer").hidden = true;
  $("#shell").classList.remove("viewer-open");
  viewer.docId = null;
}
function openSource(src) {
  const sn = $("#snippet");
  sn.hidden = false;
  sn.replaceChildren(el("div", { class: "snippet-head" }, `[${src.n}] · page ${src.page} · ${src.filename}`), el("p", {}, (src.text || src.snippet || "").slice(0, 600)));
  openDocViewer(src.doc_id, src.page, src.text || src.snippet || "", src.filename);
}
async function openDocViewer(docId, page, text = "", filename = "") {
  if (text === "" ) $("#snippet").hidden = true;
  showViewer();
  const d = docById(docId);
  $("#viewer-name").textContent = d?.filename || filename || "Document";
  viewer.text = text;
  const holder = $("#pdf-page");
  if (viewer.pdfFor !== docId) {
    holder.replaceChildren(el("div", { class: "pdf-loading" }, el("span", { class: "thinking" }, el("i"), el("i"), el("i"))));
    holder.style.width = "100%"; holder.style.height = "";
    const rec = await safeIdb(() => idb.get("docs", docId), null);
    if (!rec?.blob) {
      viewer.pdf = null; viewer.pdfFor = null; viewer.docId = docId;
      holder.style.width = "";
      holder.replaceChildren(el("div", { class: "pdf-msg" }, el("p", {}, d ? "The original PDF isn't stored in this browser, so it can't be displayed. The cited passage is shown above." : "This document is no longer in your library. The cited passage is shown above.")));
      $("#pg-label").textContent = `p. ${page}`;
      return;
    }
    try {
      const lib = await pdfLib();
      if (viewer.pdf) { viewer.pdf.destroy(); viewer.pdf = null; }
      viewer.pdf = await lib.getDocument({ data: new Uint8Array(await rec.blob.arrayBuffer()), isEvalSupported: false }).promise;
      viewer.pdfFor = docId;
    } catch (e) {
      console.warn("pdf.js:", e);
      holder.style.width = "";
      holder.replaceChildren(el("div", { class: "pdf-msg" }, el("p", {}, "Couldn't load the PDF viewer. The cited passage is shown above.")));
      return;
    }
  }
  viewer.docId = docId;
  viewer.page = clamp(page || 1, 1, viewer.pdf.numPages);
  await renderPage();
}
async function renderPage() {
  if (!viewer.pdf) return;
  const token = ++viewer.token;
  const page = await viewer.pdf.getPage(viewer.page);
  if (token !== viewer.token) return;
  const scroll = $("#pdf-scroll");
  const base = page.getViewport({ scale: 1 });
  const fit = Math.max(200, scroll.clientWidth - 28) / base.width;
  const scale = fit * viewer.zoom;
  const vp = page.getViewport({ scale });
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const canvas = el("canvas", { width: Math.floor(vp.width * dpr), height: Math.floor(vp.height * dpr), "aria-label": `Page ${viewer.page}` });
  canvas.style.width = `${Math.floor(vp.width)}px`; canvas.style.height = `${Math.floor(vp.height)}px`;
  const holder = $("#pdf-page");
  const layer = el("div", { class: "textLayer" });
  holder.style.width = `${Math.floor(vp.width)}px`; holder.style.height = `${Math.floor(vp.height)}px`;
  holder.style.setProperty("--scale-factor", String(scale));
  holder.replaceChildren(canvas, layer);
  $("#pg-label").textContent = `${viewer.page} / ${viewer.pdf.numPages}`;
  $("#pg-prev").disabled = viewer.page <= 1; $("#pg-next").disabled = viewer.page >= viewer.pdf.numPages;
  try {
    await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp, transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null }).promise;
    if (token !== viewer.token) return;
    const tl = new viewer.lib.TextLayer({ textContentSource: page.streamTextContent(), container: layer, viewport: vp });
    await tl.render();
    if (token !== viewer.token) return;
    const first = highlight(layer, viewer.text);
    scroll.scrollTop = first ? Math.max(0, first.offsetTop - 90) : 0;
  } catch (e) { if (e?.name !== "RenderingCancelledException") console.warn("render:", e); }
}
function highlight(layer, text) {
  if (!text) return null;
  const norm = (s) => s.toLowerCase().normalize("NFKD").replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  const target = ` ${norm(text)} `;
  let first = null, prevHit = false;
  for (const span of $$("span:not(.markedContent)", layer)) {
    const t = norm(span.textContent || "");
    if (!t) continue;
    const hit = target.includes(` ${t} `) || (t.length >= 12 && target.includes(t));
    if (hit && (t.length >= 8 || prevHit || t.split(" ").length >= 2)) { span.classList.add("hl"); first ||= span; prevHit = true; }
    else prevHit = false;
  }
  return first;
}

/* ---------------- dialogs ---------------- */
function promptDialog({ title, text = "", value = "", ok = "Save", input = true }) {
  const dlg = $("#dlg-prompt");
  $("#prompt-title").textContent = title;
  $("#prompt-text").textContent = text; $("#prompt-text").hidden = !text;
  const inp = $("#prompt-input"); inp.hidden = !input; inp.value = value;
  $("#prompt-ok").textContent = ok;
  dlg.returnValue = "";
  dlg.showModal();
  if (input) { inp.focus(); inp.select(); } else $("#prompt-ok").focus();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok" ? (input ? inp.value : true) : null), { once: true });
  });
}

/* ---------------- theme ---------------- */
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  $("#theme-btn").replaceChildren(icon(t === "light" ? "sun" : t === "dark" ? "moon" : "auto"));
  $("#theme-btn").title = `Theme: ${t}`;
  localStorage.setItem("dc-theme", t);
}

/* ---------------- modes: Chat | Exam Prep (one layout, one library) ---------------- */
let mode = "chat";
let examMod = null;
function setMode(next, { history: hist = "replace" } = {}) {
  next = next === "exam" ? "exam" : "chat";
  const changed = next !== mode;
  mode = next;
  $("#shell").dataset.mode = mode;
  $("#chat-pane").hidden = mode !== "chat";
  $("#exam-pane").hidden = mode !== "exam";
  for (const t of $$(".ms-tab")) { const on = t.dataset.mode === mode; t.setAttribute("aria-selected", String(on)); t.tabIndex = on ? 0 : -1; }
  for (const t of $$(".mode, .tb-item[data-mode]")) {
    if (t.dataset.mode === mode) t.setAttribute("aria-current", "page"); else t.removeAttribute("aria-current");
  }
  if (mode === "exam") {
    $("#conv-title").textContent = "Exam Prep";
    document.title = "Exam Prep · DocuChat";
    toggleScopePop(false);
    if (!$("#viewer").hidden) closeViewer();
    (examMod ? Promise.resolve(examMod) : import("./exam.js").then((m) => (examMod = m)))
      .then((m) => m.examShown())
      .catch((e) => { console.error(e); toast("Exam Prep couldn't load. Check your connection and reload.", "error", { ms: 9000 }); });
  } else if (changed) {
    renderThread();
  }
  if (changed || hist !== "none") {
    const url = new URL(location.href);
    if (mode === "exam") url.searchParams.set("mode", "exam"); else url.searchParams.delete("mode");
    url.searchParams.delete("resume");
    const target = url.pathname + url.search + url.hash;
    if (target !== location.pathname + location.search + location.hash) {
      if (hist === "push") history.pushState({ mode }, "", target); else history.replaceState({ mode }, "", target);
    }
  }
}
async function reloadDocsFromStorage() {
  const docs = await safeIdb(() => idb.all("docs"), []);
  const byId = new Map(state.docs.map((d) => [d.id, d]));
  for (const { blob, index, ...meta } of docs || []) {
    if (!byId.has(meta.id)) state.docs.push({ ...meta, status: index ? "ready" : "stale", hasBlob: !!blob });
  }
  state.selected = new Set(JSON.parse(localStorage.getItem("dc-selected") || "[]"));
  renderDocs();
}
/** Signed in: bring account chats into this browser (newest wins) and upload chats only this browser has. */
async function syncChats() {
  if (!window.dcAuth?.user) return;
  const remote = await window.dcAuth.db.listChats();
  if (!Array.isArray(remote)) return;
  const local = new Map(state.convs.map((c) => [c.id, c]));
  const remoteIds = new Set(remote.map((r) => r.id));
  let changed = false;
  for (const r of remote) {
    const l = local.get(r.id);
    if (!l || (r.updatedAt || 0) > (l.updatedAt || 0) + 1000) {
      const conv = { ...(l || {}), ...r };
      delete conv.synced;
      await safeIdb(() => idb.put("convs", conv));
      if (l) Object.assign(l, conv); else state.convs.push(conv);
      changed = true;
    }
  }
  for (const c of state.convs) if (!remoteIds.has(c.id) && c.messages?.length) window.dcAuth.db.saveChat(c);
  if (changed) {
    state.convs.sort((a, b) => b.updatedAt - a.updatedAt);
    renderConvs();
    if (state.current && !state.current.messages.length && mode === "chat") renderThread();
  }
}

/* ---------------- layout helpers ---------------- */
const mobile = () => window.matchMedia("(max-width: 860px)").matches;
function toggleSidebar() {
  const shell = $("#shell");
  if (mobile()) { shell.classList.toggle("side-open"); $("#backdrop").hidden = !shell.classList.contains("side-open"); }
  else { shell.classList.toggle("side-collapsed"); localStorage.setItem("dc-side", shell.classList.contains("side-collapsed") ? "0" : "1"); }
}
function closeSidebarMobile() { if (mobile()) { $("#shell").classList.remove("side-open"); $("#backdrop").hidden = true; } }
function autosize() { const t = $("#input"); t.style.height = "auto"; t.style.height = `${Math.min(t.scrollHeight, 220)}px`; updateSend(); }

/* ---------------- events ---------------- */
function bindEvents() {
  $("#new-chat").addEventListener("click", startNewChat);
  // mode switches: header tabs (with arrow keys), sidebar links (real hrefs work without JS), mobile tab bar
  for (const t of $$(".ms-tab, .tb-item[data-mode]")) t.addEventListener("click", () => { setMode(t.dataset.mode, { history: "push" }); closeSidebarMobile(); });
  for (const a of $$(".mode")) a.addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button) return;
    e.preventDefault(); setMode(a.dataset.mode, { history: "push" }); closeSidebarMobile();
  });
  $(".mode-switch").addEventListener("keydown", (e) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    const next = e.key === "ArrowLeft" || e.key === "Home" ? "chat" : "exam";
    setMode(next, { history: "push" }); $(`#tab-${next}`).focus();
  });
  $("#tb-library").addEventListener("click", toggleSidebar);
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-goto]");
    if (!a || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault(); setMode(a.dataset.goto, { history: "push" });
  });
  window.addEventListener("popstate", () => setMode(new URLSearchParams(location.search).get("mode"), { history: "none" }));
  document.addEventListener("dc:library-changed", reloadDocsFromStorage);
  document.addEventListener("dc:open-page", (e) => { setMode("chat", { history: "push" }); openDocViewer(e.detail.docId, e.detail.page || 1); });
  $("#chat-search").addEventListener("input", debounce((e) => { state.search = e.target.value; renderConvs(); }, 120));
  $("#upload-btn").addEventListener("click", () => $("#file-input").click());
  $("#mini-drop").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#file-input").click(); } });
  $("#file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  $("#collapse-side").addEventListener("click", toggleSidebar);
  $("#open-side").addEventListener("click", toggleSidebar);
  $("#backdrop").addEventListener("click", closeSidebarMobile);
  $("#conv-title").addEventListener("dblclick", () => state.current?.messages.length && renameConv(state.current));
  $("#scope-btn").addEventListener("click", (e) => { e.stopPropagation(); toggleScopePop(); });
  document.addEventListener("click", (e) => { if (!e.target.closest("#scope-pop, #scope-btn")) toggleScopePop(false); });
  $("#keys-btn").addEventListener("click", () => $("#dlg-keys").showModal());
  $("#theme-btn").addEventListener("click", () => applyTheme(THEMES[(THEMES.indexOf(localStorage.getItem("dc-theme") || "auto") + 1) % 3]));
  $("#tools").addEventListener("click", (e) => { const b = e.target.closest(".tool"); if (b && !b.disabled) runTool(b.dataset.tool); });
  // fade the toolbar edge that has more tools hidden behind it
  const tools = $("#tools");
  const edge = () => {
    tools.classList.toggle("more-left", tools.scrollLeft > 2);
    tools.classList.toggle("more-right", tools.scrollLeft + tools.clientWidth < tools.scrollWidth - 2);
  };
  tools.addEventListener("scroll", edge, { passive: true });
  if ("ResizeObserver" in window) new ResizeObserver(edge).observe(tools); else window.addEventListener("resize", edge);
  edge();

  const input = $("#input");
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    if (state.busy) { state.abort?.abort(); return; }
    const q = input.value.trim(); if (!q) return;
    if (!ensureCanAsk()) return;
    input.value = ""; autosize();
    sendChat(q);
  });

  // viewer
  $("#viewer-close").addEventListener("click", closeViewer);
  $("#pg-prev").addEventListener("click", () => { if (viewer.pdf && viewer.page > 1) { viewer.page--; viewer.text = ""; renderPage(); } });
  $("#pg-next").addEventListener("click", () => { if (viewer.pdf && viewer.page < viewer.pdf.numPages) { viewer.page++; viewer.text = ""; renderPage(); } });
  $("#zoom-in").addEventListener("click", () => { viewer.zoom = clamp(viewer.zoom * 1.2, 0.5, 3); renderPage(); });
  $("#zoom-out").addEventListener("click", () => { viewer.zoom = clamp(viewer.zoom / 1.2, 0.5, 3); renderPage(); });
  window.addEventListener("resize", debounce(() => { if (!$("#viewer").hidden) renderPage(); }, 200));
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !state.busy) ensureSynced(); });

  // drag and drop anywhere
  let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  window.addEventListener("dragenter", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth++; $("#drop-overlay").hidden = false; });
  window.addEventListener("dragover", (e) => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener("dragleave", (e) => { if (!hasFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) $("#drop-overlay").hidden = true; });
  window.addEventListener("drop", (e) => { if (!hasFiles(e)) return; e.preventDefault(); depth = 0; $("#drop-overlay").hidden = true; addFiles(e.dataTransfer.files); });

  // selection toolbar on answers
  const bar = $("#sel-bar");
  let selText = "";
  document.addEventListener("mouseup", (e) => {
    if (e.target.closest("#sel-bar")) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel?.toString().trim() || "";
      const anchor = sel?.anchorNode?.parentElement;
      if (text.length < 3 || !anchor?.closest(".bot-body") || state.busy) { bar.hidden = true; return; }
      selText = text.slice(0, 500);
      const r = sel.getRangeAt(0).getBoundingClientRect();
      bar.hidden = false;
      bar.style.top = `${Math.max(8, r.top - 46)}px`;
      bar.style.left = `${clamp(r.left + r.width / 2 - bar.offsetWidth / 2, 8, window.innerWidth - bar.offsetWidth - 8)}px`;
    }, 0);
  });
  $("#thread").addEventListener("scroll", () => { bar.hidden = true; }, { passive: true });
  bar.addEventListener("click", (e) => {
    const act = e.target.closest("button")?.dataset.act; if (!act) return;
    bar.hidden = true; window.getSelection()?.removeAllRanges();
    if (act === "ask") { input.value = `About "${selText}": `; autosize(); input.focus(); input.setSelectionRange(input.value.length, input.value.length); }
    else runTool("explain", { topic: selText });
  });

  // keyboard shortcuts
  document.addEventListener("keydown", (e) => {
    const mod = isMac ? e.metaKey : e.ctrlKey;
    const typing = e.target.closest("input, textarea, select, [contenteditable]");
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); if (mobile()) toggleSidebar(); $("#chat-search").focus(); }
    else if (mod && e.shiftKey && e.key.toLowerCase() === "o") { e.preventDefault(); startNewChat(); }
    else if (mod && !e.shiftKey && e.key.toLowerCase() === "u") { e.preventDefault(); $("#file-input").click(); }
    else if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSidebar(); }
    else if (e.key === "Escape") {
      if (document.querySelector("dialog[open]")) return;
      if (!$("#scope-pop").hidden) toggleScopePop(false);
      else if (!$("#sel-bar").hidden) $("#sel-bar").hidden = true;
      else if ($("#shell").classList.contains("side-open")) closeSidebarMobile();
      else if (!$("#viewer").hidden) closeViewer();
      else if (typing) e.target.blur();
    }
    else if (!typing && !mod && e.key === "/" && mode === "chat") { e.preventDefault(); input.focus(); }
    else if (!typing && !mod && e.key === "?") { e.preventDefault(); $("#dlg-keys").showModal(); }
  });
  for (const k of $$(".kbd-hint")) k.textContent = k.textContent.replace("Ctrl", isMac ? "⌘" : "Ctrl");
}

/* ---------------- boot ---------------- */
async function boot() {
  applyTheme(localStorage.getItem("dc-theme") || "auto");
  if (localStorage.getItem("dc-side") === "0" && !mobile()) $("#shell").classList.add("side-collapsed");
  bindEvents();
  const [convs, docs] = await Promise.all([safeIdb(() => idb.all("convs"), []), safeIdb(() => idb.all("docs"), [])]);
  state.convs = (convs || []).sort((a, b) => b.updatedAt - a.updatedAt);
  state.docs = (docs || []).sort((a, b) => a.addedAt - b.addedAt).map(({ blob, index, ...meta }) => ({ ...meta, status: index ? "ready" : "stale", hasBlob: !!blob }));
  if (state.selectAll) { state.docs.forEach((d) => state.selected.add(d.id)); saveSelection(); }
  const known = new Set(state.docs.map((d) => d.id));
  for (const id of [...state.selected]) if (!known.has(id)) state.selected.delete(id);
  const last = state.convs.find((c) => c.id === localStorage.getItem("dc-last"));
  state.current = last && Date.now() - last.updatedAt < 6 * 3600e3 ? last : newConv();
  renderConvs(); renderDocs(); renderThread();
  setMode(new URLSearchParams(location.search).get("mode"), { history: "none" });
  try {
    state.config = await apiJson("/api/config");
    if (state.config.provider === "FakeProvider") toast("Offline demo mode: answers are extracts, not AI-written.", "info", { ms: 6000 });
  } catch (e) { handleError(e); }
  await refreshUsage();
  renderDocs();
  await syncDocs();
  if (mode === "chat") { renderThread(); if (!mobile()) $("#input").focus({ preventScroll: true }); }
  if (window.dcAuth?.ready) window.dcAuth.ready.then(syncChats);
}
boot().catch((e) => { console.error(e); toast("DocuChat failed to start. Please reload the page.", "error", { ms: 10000 }); });
