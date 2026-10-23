// DocuChat /login: sign in, sign up (with email confirmation), forgot / reset password. Uses window.dcAuth (auth.js).
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const params = new URLSearchParams(location.search);
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));   // read before supabase-js consumes it
  const rawNext = params.get("next") || "/app";
  const next = /^\/(?!\/)[^\s]*$/.test(rawNext) && !rawNext.startsWith("/login") ? rawNext : "/app";
  let tab = params.get("tab") === "signup" ? "signup" : "signin";
  const auth = window.dcAuth;

  const COPY = {
    signin: { title: "Welcome back", sub: "Sign in to pick up where you left off.", submit: "Sign in", autocomplete: "current-password" },
    signup: { title: "Create your free account", sub: "Save your exam scores and chats on every device.", submit: "Create account", autocomplete: "new-password" },
  };

  function friendly(err) {
    const m = String(err?.message || err || "").toLowerCase();
    if (m.includes("invalid login")) return "That email and password don't match. Check them and try again.";
    if (m.includes("not confirmed")) return "Please confirm your email first: open the link we sent you (check spam too).";
    if (m.includes("already registered") || m.includes("already exists")) return "An account with this email already exists. Sign in instead.";
    if (m.includes("rate limit") || m.includes("too many") || m.includes("security purposes")) return "Too many attempts. Please wait a minute and try again.";
    if (m.includes("password") && (m.includes("6") || m.includes("short") || m.includes("weak"))) return "Use a password with at least 6 characters.";
    if (m.includes("invalid") && m.includes("email")) return "Enter a valid email address.";
    if (m.includes("failed to fetch") || m.includes("network") || m.includes("load")) return "Can't reach the sign-in service. Check your connection and try again.";
    return err?.message || "Something went wrong. Please try again.";
  }
  const showErr = (id, msg, info = false) => { const e = $(id); e.textContent = msg || ""; e.hidden = !msg; e.classList.toggle("info", info); };
  function busy(btn, on) { btn.disabled = on; btn.setAttribute("aria-busy", String(on)); }
  function view(name) {
    for (const id of ["form-auth", "form-forgot", "form-reset", "panel-msg"]) $(`#${id}`).hidden = id !== name;
    $(".login-tabs").hidden = name !== "form-auth";
    $(".login-head").hidden = name === "panel-msg";
  }
  function message(title, text, actions = []) {
    $("#msg-title").textContent = title;
    $("#msg-text").textContent = text;
    $("#msg-acts").replaceChildren(...actions);
    view("panel-msg");
  }
  const action = (label, { href, primary, onclick } = {}) => {
    const n = document.createElement(href ? "a" : "button");
    n.className = `btn btn-lg ${primary ? "btn-primary" : "btn-ghost"}`;
    n.textContent = label;
    if (href) n.href = href; else { n.type = "button"; n.addEventListener("click", onclick); }
    return n;
  };

  function setTab(t, focus = false) {
    tab = t;
    for (const b of document.querySelectorAll(".login-tabs button")) {
      const on = b.dataset.tab === t;
      b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1;
      if (on && focus) b.focus();
    }
    const c = COPY[t];
    $("#login-title").textContent = c.title;
    $("#login-sub").textContent = c.sub;
    $("#auth-submit span").textContent = c.submit;
    $("#password").autocomplete = c.autocomplete;
    $("#forgot-link").hidden = t === "signup";
    $("#signup-fine").hidden = t !== "signup";
    $("#form-auth").setAttribute("aria-labelledby", `tab-${t}`);
    document.title = `${t === "signup" ? "Sign up" : "Sign in"} · DocuChat`;
    showErr("#auth-error", "");
    const u = new URL(location.href); if (t === "signup") u.searchParams.set("tab", "signup"); else u.searchParams.delete("tab");
    history.replaceState(null, "", u.pathname + u.search);
  }

  function validEmail(v) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v); }
  function check(emailEl, passEl) {
    const email = emailEl.value.trim();
    emailEl.setAttribute("aria-invalid", String(!validEmail(email)));
    if (!validEmail(email)) { emailEl.focus(); return "Enter a valid email address."; }
    if (passEl) {
      const bad = passEl.value.length < 6;
      passEl.setAttribute("aria-invalid", String(bad));
      if (bad) { passEl.focus(); return "Use a password with at least 6 characters."; }
    }
    return null;
  }
  const go = () => { location.href = next; };

  async function resend(email, btn) {
    btn.disabled = true;
    try {
      const { error } = await auth.client.auth.resend({ type: "signup", email, options: { emailRedirectTo: `${location.origin}/login?next=${encodeURIComponent(next)}` } });
      if (error) throw error;
      btn.textContent = "Sent again ✓";
    } catch (e) { btn.disabled = false; $("#msg-text").textContent = friendly(e); }
  }

  async function onAuthSubmit(e) {
    e.preventDefault();
    const err = check($("#email"), $("#password"));
    if (err) return showErr("#auth-error", err);
    showErr("#auth-error", "");
    const btn = $("#auth-submit"); busy(btn, true);
    const email = $("#email").value.trim(), password = $("#password").value;
    try {
      const client = auth.client || await auth.loadSdk();
      if (tab === "signin") {
        const { error } = await client.auth.signInWithPassword({ email, password });
        if (error) throw error;
        go();
        return;
      }
      const { data, error } = await client.auth.signUp({ email, password, options: { emailRedirectTo: `${location.origin}/login?next=${encodeURIComponent(next)}` } });
      if (error) throw error;
      if (data.session) { go(); return; }   // email confirmation is off: signed in straight away
      if (data.user && Array.isArray(data.user.identities) && data.user.identities.length === 0) {
        busy(btn, false); setTab("signin"); $("#email").value = email;
        return showErr("#auth-error", "An account with this email already exists. Sign in instead.", true);
      }
      const again = action("Resend email", { onclick: (ev) => resend(email, ev.currentTarget) });
      message("Check your email", `We sent a confirmation link to ${email}. Open it to finish creating your account, then you'll be signed in.`,
        [action("Continue as guest for now", { href: next, primary: true }), again]);
    } catch (ex) {
      const msg = friendly(ex);
      showErr("#auth-error", msg);
      if (/confirm your email/.test(msg)) {
        const b = document.createElement("button"); b.type = "button"; b.className = "lf-link"; b.textContent = " Resend confirmation";
        b.addEventListener("click", async () => {
          try { const { error } = await auth.client.auth.resend({ type: "signup", email }); if (error) throw error; showErr("#auth-error", `Confirmation link sent to ${email}.`, true); }
          catch (er) { showErr("#auth-error", friendly(er)); }
        });
        $("#auth-error").append(b);
      }
    } finally { busy(btn, false); }
  }

  async function onForgot(e) {
    e.preventDefault();
    const err = check($("#forgot-email"));
    if (err) return showErr("#forgot-error", err);
    const btn = $("#forgot-submit"); busy(btn, true);
    const email = $("#forgot-email").value.trim();
    try {
      const client = auth.client || await auth.loadSdk();
      const { error } = await client.auth.resetPasswordForEmail(email, { redirectTo: `${location.origin}/login?reset=1&next=${encodeURIComponent(next)}` });
      if (error) throw error;
      message("Check your email", `If an account exists for ${email}, a link to set a new password is on its way.`,
        [action("Back to sign in", { primary: true, onclick: () => { view("form-auth"); setTab("signin"); } })]);
    } catch (ex) { showErr("#forgot-error", friendly(ex)); }
    finally { busy(btn, false); }
  }

  async function onReset(e) {
    e.preventDefault();
    const p = $("#new-password");
    if (p.value.length < 6) { p.setAttribute("aria-invalid", "true"); return showErr("#reset-error", "Use a password with at least 6 characters."); }
    const btn = $("#reset-submit"); busy(btn, true);
    try {
      const { error } = await auth.client.auth.updateUser({ password: p.value });
      if (error) throw error;
      message("Password updated", "You're signed in with your new password.", [action("Continue to DocuChat", { href: next, primary: true })]);
    } catch (ex) { showErr("#reset-error", friendly(ex)); }
    finally { busy(btn, false); }
  }

  function bind() {
    for (const b of document.querySelectorAll(".login-tabs button")) b.addEventListener("click", () => setTab(b.dataset.tab));
    $(".login-tabs").addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); setTab(tab === "signin" ? "signup" : "signin", true); }
    });
    $("#form-auth").addEventListener("submit", onAuthSubmit);
    $("#form-forgot").addEventListener("submit", onForgot);
    $("#form-reset").addEventListener("submit", onReset);
    $("#forgot-link").addEventListener("click", () => { $("#forgot-email").value = $("#email").value.trim(); view("form-forgot"); showErr("#forgot-error", ""); $("#forgot-email").focus(); });
    for (const b of document.querySelectorAll("[data-back]")) b.addEventListener("click", () => { view("form-auth"); setTab("signin"); });
    $("#toggle-pass").addEventListener("click", (e) => {
      const p = $("#password"), show = p.type === "password";
      p.type = show ? "text" : "password";
      e.currentTarget.textContent = show ? "Hide" : "Show";
      e.currentTarget.setAttribute("aria-pressed", String(show));
      e.currentTarget.setAttribute("aria-label", show ? "Hide password" : "Show password");
    });
    for (const i of document.querySelectorAll("input")) i.addEventListener("input", () => i.removeAttribute("aria-invalid"));
  }

  async function boot() {
    bind();
    setTab(tab);
    view("form-auth");
    if (!auth?.enabled) { $("#auth-off").hidden = false; for (const b of document.querySelectorAll(".lf-submit")) b.disabled = true; return; }
    const hashErr = hash.get("error_description");
    const kind = hash.get("type");
    let recovering = kind === "recovery" || params.get("reset") === "1" && hash.has("access_token");
    try { await auth.loadSdk(); } catch (e) { showErr("#auth-error", friendly(e)); return; }
    auth.client.auth.onAuthStateChange((event) => { if (event === "PASSWORD_RECOVERY") { recovering = true; view("form-reset"); $("#new-password").focus(); } });
    await auth.ready;
    if (hashErr) {
      showErr("#auth-error", /expired|invalid/i.test(hashErr) ? "That link has expired or was already used. Request a new one below." : hashErr);
      history.replaceState(null, "", location.pathname + location.search);
      return;
    }
    if (recovering && auth.user) { view("form-reset"); $("#new-password").focus(); return; }
    if (auth.user && (kind === "signup" || kind === "magiclink" || kind === "email")) {
      message("Email confirmed", `You're signed in as ${auth.user.email}. Taking you to DocuChat…`, [action("Continue", { href: next, primary: true })]);
      setTimeout(go, 1400);
      return;
    }
    if (auth.user) {
      message("You're signed in", `Signed in as ${auth.user.email}.`, [
        action("Continue to DocuChat", { href: next, primary: true }),
        action("Sign out", { onclick: () => auth.signOut().then(() => { view("form-auth"); setTab("signin"); }) })]);
      return;
    }
    (tab === "signup" || !$("#email").value ? $("#email") : $("#password")).focus({ preventScroll: true });
  }
  boot().catch((e) => { console.error(e); showErr("#auth-error", friendly(e)); });
})();
