"use strict";
/* Ready Signal widget. Runs inside an iframe on an authorised parent dashboard.
   Credential handling: never in a URL. It is either typed/pasted here once (kept in this frame's localStorage) or sent by
   the parent via postMessage from the authorised origin. Requests carry it as a Bearer header. */
const { h, mount, fmtRemaining, fmtDuration, iconEl } = RS;
const root = document.getElementById("w"), ORIGIN = document.documentElement.dataset.origin, KEY = "readysignal.widget.credential";
const W = { token: null, accounts: [], skew: 0, opts: {}, error: "", busy: new Set(), copyFallback: null };
let timer = null, poll = null;
const store = { get() { try { return localStorage.getItem(KEY); } catch { return null; } }, set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch { /* storage unavailable */ } } };
W.token = store.get();

async function call(method, path) {
  const res = await fetch("/widget/api" + path, { method, headers: { Authorization: "Bearer " + W.token }, cache: "no-store" });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (res.status === 401) { W.token = null; store.set(null); const e = new Error(data?.detail || "Not authorised"); e.auth = true; throw e; }
  if (!res.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`);
  return data;
}
const nowMs = () => Date.now() + W.skew;

function connectForm(message) {
  const input = h("input", { type: "password", autocomplete: "off", required: true, "aria-label": "Widget credential", placeholder: "rsw_…" });
  mount(root, h("form", { class: "connect", onsubmit: ev => { ev.preventDefault(); const v = input.value.trim(); if (!v) return; W.token = v; store.set(v); refresh(); } },
    h("p", {}, "Enter the widget credential from Ready Signal → Settings."), input,
    message ? h("p", { class: "err", role: "alert" }, message) : null, h("button", { class: "primary", type: "submit" }, "Connect")));
}

function row(a) {
  const cooling = a.status === "cooling", busy = W.busy.has(a.id);
  return h("article", { class: "wrow", "aria-label": a.name },
    iconEl(a, "icon sm"),
    h("div", { class: "winfo" }, h("div", { class: "wname" }, h("strong", {}, a.name), h("span", { class: "chip " + (cooling ? "cool" : "ok") }, cooling ? "Cooling down" : "Available")),
      h("div", { class: "wtime" + (cooling ? "" : " ready"), ...(cooling ? { "data-expiry": a.ts_expired, role: "timer" } : {}) }, cooling ? "…" : "Ready"),
      W.opts.show_project && a.project ? h("div", { class: "wproj" }, a.project) : null),
    h("div", { class: "wact" },
      h("button", { class: "primary", disabled: busy, "aria-busy": busy ? "true" : "false", onclick: () => start(a) }, busy ? "Working…" : cooling ? "Extend +" + fmtDuration(a.duration_seconds) : "Start"),
      a.has_prompt ? h("button", { disabled: busy, "aria-label": "Copy continuation prompt for " + a.name, onclick: () => copy(a) }, "Copy prompt") : null));
}
function render() {
  mount(root, W.error ? h("p", { class: "err pad", role: "alert" }, W.error) : null,
    W.copyFallback ? h("div", { class: "pad" }, h("p", { class: "hint" }, "Your browser blocked clipboard access in this frame. Select and copy the text below (the embedding page needs allow=\"clipboard-write\")."),
      (() => { const t = h("textarea", { readonly: true, "aria-label": "Prompt text" }); t.value = W.copyFallback; setTimeout(() => t.select(), 0); return t; })(),
      h("button", { onclick: () => { W.copyFallback = null; render(); } }, "Close")) : null,
    W.accounts.length ? W.accounts.map(row) : h("p", { class: "sub pad" }, "No accounts are shared with this widget."));
  tick();
}
function tick() {
  const now = nowMs(); let lapsed = false;
  document.querySelectorAll("[data-expiry]").forEach(el => { const left = Date.parse(el.dataset.expiry) - now; if (left <= 0) { lapsed = true; el.textContent = "Ready"; } else el.textContent = fmtRemaining(left); });
  if (lapsed && !tick.pending) { tick.pending = true; setTimeout(() => { tick.pending = false; refresh(); }, 1500); }
}
async function refresh(keepError = false) {
  if (!W.token) return connectForm("");
  try {
    const d = await call("GET", "/summary");
    W.accounts = d.accounts; W.opts = d; W.skew = Date.parse(d.now) - Date.now(); if (!keepError) W.error = "";
    render(); clearInterval(timer); timer = setInterval(tick, 1000);
    clearInterval(poll); poll = setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  } catch (e) { if (e.auth) { clearInterval(timer); clearInterval(poll); connectForm(e.message); } else { W.error = e.message; render(); } }
}
async function start(a) {
  W.busy.add(a.id); W.error = ""; render();
  try { await call("POST", `/accounts/${encodeURIComponent(a.id)}/start`); } catch (e) { if (e.auth) return connectForm(e.message); W.error = `${a.name}: ${e.message}`; }
  W.busy.delete(a.id); await refresh(!!W.error);  // keep an action error visible after the summary reload
}
async function copy(a) {
  W.busy.add(a.id); W.error = ""; render();
  try {
    const { prompt } = await call("GET", `/accounts/${encodeURIComponent(a.id)}/prompt`);
    try { await navigator.clipboard.writeText(prompt); W.error = ""; }
    catch { W.copyFallback = prompt; }  // permission denied / unsupported context: let the person copy manually
  } catch (e) { if (e.auth) return connectForm(e.message); W.error = `${a.name}: ${e.message}`; }
  W.busy.delete(a.id); render();
}

/* Optional handshake: the authorised parent may deliver the credential without any user typing. */
window.addEventListener("message", ev => {
  if (ev.origin !== ORIGIN || ev.source !== window.parent) return;  // wrong sender: ignore silently
  const d = ev.data;
  if (d && d.type === "ready-signal:credential" && typeof d.credential === "string" && d.credential) { W.token = d.credential; refresh(); }
});
try { if (window.parent !== window) window.parent.postMessage({ type: "ready-signal:ready" }, ORIGIN); } catch { /* not framed */ }
document.addEventListener("visibilitychange", () => { if (!document.hidden && W.token) { tick(); refresh(); } });
refresh();
