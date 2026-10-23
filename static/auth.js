// DocuChat accounts (Supabase Auth). Classic script, loaded before the page's own module on every page.
// - Adds the signed-in user's access token to same-origin /api/ calls (fetch and XHR), so the server keys quota,
//   PDFs and limits by user id. Guests are untouched: no account is ever required.
// - Renders the account control into every [data-auth-slot] and the "save your progress" nudge into [data-auth-nudge].
// - window.dcAuth.db reads/writes the user's own rows (exam_results, saved_chats) straight from the browser; Row Level
//   Security on those tables (auth.uid() = user_id) means a session can only ever touch its own rows.
// supabase-js (~220 KB) loads only where needed (app + login); the landing page reads the stored session directly.
(() => {
  "use strict";
  const CFG = window.DC_AUTH || {};
  const SDK = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.118.0/dist/umd/supabase.js";
  const enabled = !!(CFG.url && CFG.key);
  let ref = "";
  try { ref = new URL(CFG.url).hostname.split(".")[0]; } catch { /* disabled */ }
  const STORE_KEY = `sb-${ref}-auth-token`;
  const NUDGE_KEY = "dc-nudge-hidden";
  const listeners = new Set();

  const auth = {
    enabled, client: null, user: null, session: null,
    ready: null,
    token: () => auth.session?.access_token || null,
    onChange: (fn) => { listeners.add(fn); return () => listeners.delete(fn); },
    signOut, loadSdk, render, db: {},
  };
  window.dcAuth = auth;

  /* ---------- stored session (no SDK needed: used by the landing page and for a fast first paint) ---------- */
  function storedSession() {
    if (!enabled) return null;
    try {
      const s = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
      return s?.access_token && s?.user ? s : null;
    } catch { return null; }
  }
  function setSession(s) {
    auth.session = s || null;
    auth.user = s?.user ? { id: s.user.id, email: s.user.email } : null;
    render();
    for (const fn of listeners) { try { fn(auth.user); } catch (e) { console.warn(e); } }
  }

  /* ---------- SDK ---------- */
  let sdkPromise = null;
  function loadSdk() {
    if (!enabled) return Promise.resolve(null);
    return sdkPromise ||= new Promise((resolve, reject) => {
      if (window.supabase?.createClient) return resolve(window.supabase);
      const s = document.createElement("script");
      s.src = SDK; s.async = true; s.crossOrigin = "anonymous";
      s.onload = () => resolve(window.supabase);
      s.onerror = () => reject(new Error("Couldn't load the sign-in service."));
      document.head.append(s);
    }).then((lib) => {
      if (!auth.client) {
        auth.client = lib.createClient(CFG.url, CFG.key, {
          auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true, flowType: "implicit" },
        });
        let first = true;
        auth.client.auth.onAuthStateChange((event, session) => {
          const before = auth.user?.id || null;
          setSession(session);
          const after = auth.user?.id || null;
          // identity changed in this or another tab: reload app pages so the server-side workspace switches cleanly
          if (!first && before !== after && document.body.dataset.authReload !== "off") setTimeout(() => location.reload(), 50);
          first = false;
          document.dispatchEvent(new CustomEvent("dc:auth", { detail: { event, user: auth.user } }));
        });
      }
      return auth.client;
    });
  }

  const wantsSdk = document.currentScript?.dataset.sdk !== "lazy";

  async function signOut() {
    try { const c = await loadSdk(); await c?.auth.signOut(); }
    catch (e) { console.warn("sign out:", e?.message || e); }
    try { localStorage.removeItem(STORE_KEY); } catch { /* ignore */ }
    setSession(null);
    if (document.body.dataset.authReload === "off") return;
    location.reload();
  }

  /* ---------- attach the token to our own API calls ---------- */
  const isApi = (url) => {
    try { const u = new URL(url, location.href); return u.origin === location.origin && u.pathname.startsWith("/api/"); }
    catch { return false; }
  };
  const withTimeout = (p, ms) => Promise.race([p, new Promise((r) => setTimeout(r, ms))]);
  if (enabled && window.fetch) {
    const origFetch = window.fetch.bind(window);
    window.fetch = async (input, init = {}) => {
      const url = typeof input === "string" ? input : input?.url;
      if (!url || !isApi(url)) return origFetch(input, init);
      await withTimeout(auth.ready, 4000);
      const tok = auth.token();
      if (!tok) return origFetch(input, init);
      const headers = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
      if (!headers.has("Authorization")) headers.set("Authorization", `Bearer ${tok}`);
      return origFetch(input, { ...init, headers });
    };
    const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) { this._dcApi = isApi(url); return open.call(this, method, url, ...rest); };
    XMLHttpRequest.prototype.send = function (body) {
      const tok = this._dcApi && auth.token();
      if (tok) { try { this.setRequestHeader("Authorization", `Bearer ${tok}`); } catch { /* already sent */ } }
      return send.call(this, body);
    };
  }

  /* ---------- per-user data (Supabase REST via supabase-js, protected by RLS) ---------- */
  async function table(name) {
    if (!auth.user) return null;
    const c = await loadSdk();
    return c ? c.from(name) : null;
  }
  const quiet = (label) => (e) => { console.warn(`${label}:`, e?.message || e); return null; };
  auth.db = {
    async saveExam(entry) {
      const t = await table("exam_results"); if (!t) return null;
      const meta = { ...entry }; delete meta.score; delete meta.max;
      if (JSON.stringify(meta).length > 900_000) delete meta.attempt;   // keep rows reasonable
      const { error } = await t.upsert({ id: String(entry.id), user_id: auth.user.id, score: entry.score || 0, total: entry.max || 0,
        style: entry.style || null, meta, created_at: new Date(entry.date || Date.now()).toISOString() }, { onConflict: "user_id,id" });
      if (error) throw error;
      return true;
    },
    async listExams(limit = 50) {
      const t = await table("exam_results"); if (!t) return [];
      const { data, error } = await t.select("id,created_at,score,total,style,meta").order("created_at", { ascending: false }).limit(limit);
      if (error) throw error;
      return (data || []).map((r) => ({ ...(r.meta || {}), id: r.id, score: Number(r.score), max: Number(r.total), style: r.style,
        date: r.meta?.date || Date.parse(r.created_at) }));
    },
    async clearExams() {
      const t = await table("exam_results"); if (!t) return null;
      const { error } = await t.delete().eq("user_id", auth.user.id);
      if (error) throw error;
      return true;
    },
    async saveChat(conv) {
      const t = await table("saved_chats"); if (!t || !conv?.messages?.length) return null;
      const { error } = await t.upsert({ id: String(conv.id), user_id: auth.user.id, title: (conv.title || "New chat").slice(0, 200),
        messages: conv.messages, created_at: new Date(conv.createdAt || Date.now()).toISOString(),
        updated_at: new Date(conv.updatedAt || Date.now()).toISOString() }, { onConflict: "user_id,id" });
      if (error) throw error;
      return true;
    },
    async deleteChat(id) {
      const t = await table("saved_chats"); if (!t) return null;
      const { error } = await t.delete().eq("user_id", auth.user.id).eq("id", String(id));
      if (error) throw error;
      return true;
    },
    async listChats(limit = 100) {
      const t = await table("saved_chats"); if (!t) return [];
      const { data, error } = await t.select("id,title,messages,created_at,updated_at").order("updated_at", { ascending: false }).limit(limit);
      if (error) throw error;
      return (data || []).map((r) => ({ id: r.id, title: r.title, messages: r.messages || [], createdAt: Date.parse(r.created_at), updatedAt: Date.parse(r.updated_at), synced: true }));
    },
  };
  for (const k of Object.keys(auth.db)) { const f = auth.db[k]; auth.db[k] = (...a) => f(...a).catch(quiet(`sync ${k}`)); }

  /* ---------- UI ---------- */
  const h = (tag, attrs = {}, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(String(c)));
    return n;
  };
  const here = () => location.pathname + location.search;
  const loginHref = (tab) => `/login?${tab === "signup" ? "tab=signup&" : ""}next=${encodeURIComponent(here() === "/" ? "/app" : here())}`;
  const initial = (email) => (email || "?").trim().charAt(0).toUpperCase();

  let menuOpen = null;
  function closeMenu() { if (menuOpen) { menuOpen.hidden = true; menuOpen.previousSibling?.setAttribute("aria-expanded", "false"); menuOpen = null; } }
  document.addEventListener("click", (e) => { if (menuOpen && !e.target.closest(".acct")) closeMenu(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && menuOpen) { const b = menuOpen.previousSibling; closeMenu(); b?.focus(); } });

  function slot(el) {
    const style = el.dataset.authSlot || "default";   // "compact" (app header) | "default" (landing / exam)
    if (!enabled) { el.replaceChildren(); return; }
    if (!auth.user && style === "landing") {
      el.replaceChildren(h("a", { class: "btn btn-ghost acct-signin", href: loginHref("signin") }, "Sign in"));
      return;
    }
    if (!auth.user) {
      el.replaceChildren(
        h("a", { class: `btn btn-ghost ${style === "compact" ? "btn-sm" : ""} acct-signin`, href: loginHref("signin") }, "Sign in"),
        h("a", { class: `btn btn-primary ${style === "compact" ? "btn-sm" : ""} acct-signup`, href: loginHref("signup") }, "Sign up"));
      return;
    }
    const email = auth.user.email || "Your account";
    const menu = h("div", { class: "acct-menu", role: "menu", hidden: true },
      h("div", { class: "acct-who" }, h("span", { class: "acct-label" }, "Signed in as"), h("b", { title: email }, email)),
      h("p", { class: "acct-note" }, "Your exam results and chats are saved to your account."),
      h("a", { class: "acct-item", role: "menuitem", href: "/app" }, "Open DocuChat"),
      h("button", { class: "acct-item danger", role: "menuitem", type: "button", onclick: signOut }, "Sign out"));
    const btn = h("button", { class: "acct-btn", type: "button", "aria-haspopup": "menu", "aria-expanded": "false", title: email,
      onclick: (e) => {
        e.stopPropagation();
        const open = menu.hidden;
        closeMenu();
        if (open) { menu.hidden = false; btn.setAttribute("aria-expanded", "true"); menuOpen = menu; menu.querySelector(".acct-item")?.focus(); }
      } },
      h("span", { class: "acct-avatar", "aria-hidden": "true" }, initial(email)),
      style === "compact" ? null : h("span", { class: "acct-email" }, email));
    if (style === "landing") menu.querySelector("a.acct-item").textContent = "Open DocuChat";
    el.replaceChildren(h("div", { class: "acct" }, btn, menu));
  }
  function nudge(el) {
    let hidden = 0;
    try { hidden = +localStorage.getItem(NUDGE_KEY) || 0; } catch { /* ignore */ }
    const show = enabled && !auth.user && Date.now() - hidden > 7 * 864e5;
    if (!show) { el.hidden = true; el.replaceChildren(); return; }
    el.hidden = false;
    el.replaceChildren(h("div", { class: "nudge" },
      h("div", { class: "nudge-text" }, h("b", {}, "Sign up free to save your progress"),
        h("span", {}, el.dataset.authNudge || "Keep your exam scores and chats on any device.")),
      h("div", { class: "nudge-acts" },
        h("a", { class: "btn btn-primary btn-sm", href: loginHref("signup") }, "Sign up free"),
        h("button", { class: "nudge-x", type: "button", "aria-label": "Not now", title: "Not now",
          onclick: () => { try { localStorage.setItem(NUDGE_KEY, String(Date.now())); } catch { /* ignore */ } nudge(el); } }, "×"))));
  }
  function render() {
    if (document.readyState === "loading") return;
    document.querySelectorAll("[data-auth-slot]").forEach(slot);
    document.querySelectorAll("[data-auth-nudge]").forEach(nudge);
    document.documentElement.classList.toggle("signed-in", !!auth.user);
    document.querySelectorAll("[data-signed-text]").forEach((n) => {
      n.dataset.guestText ||= n.textContent;
      n.textContent = auth.user ? n.dataset.signedText : n.dataset.guestText;
    });
  }
  /* ---------- start (after every helper above is defined) ---------- */
  setSession(storedSession());
  auth.ready = !enabled || !wantsSdk ? Promise.resolve() :
    loadSdk().then((c) => c.auth.getSession()).then(({ data }) => setSession(data.session))
      .catch((e) => { console.warn("auth:", e?.message || e); });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", render, { once: true }); else render();
})();
