"use strict";
/* Ready Signal widget: a compact, credential-free view for a dashboard on the private network.
   Countdowns are local (absolute timestamps); the backend is polled once a minute and after actions/expiry. */
const { h, mount, fmtRemaining, fmtDuration, iconEl, icon } = RS;
const root = document.getElementById("w"), over = document.getElementById("over");
const W = { accounts: [], skew: 0, opts: {}, busy: new Set() };
let timer = null, poll = null, overTimer = null;

async function call(method, path) {
  const res = await fetch("/widget/api" + path, { method, cache: "no-store" });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `Request failed (${res.status})`);
  return data;
}
const nowMs = () => Date.now() + W.skew;

function say(msg, isErr, ...extra) {
  clearTimeout(overTimer);
  if (!msg) { over.hidden = true; return; }
  over.className = isErr ? "err" : ""; over.hidden = false;
  mount(over, h("span", {}, msg), ...extra);
  if (!extra.length) overTimer = setTimeout(() => { over.hidden = true; }, 4000);
}

function row(a) {
  const cooling = a.status === "cooling", busy = W.busy.has(a.id), ext = "Extend " + a.name + " by " + fmtDuration(a.duration_seconds);
  const btn = (name, label, cls, fn) => h("button", { type: "button", class: cls, title: label, "aria-label": label, disabled: busy, "aria-busy": busy ? "true" : "false", onclick: fn }, icon(name));
  const project = W.opts.show_project ? (a.project || "").trim() : "";
  return h("article", { class: "wrow " + (cooling ? "cooling" : "available"), "aria-label": a.name },
    iconEl(a, "icon"),
    h("div", { class: "wtext" }, h("strong", { class: "wname", title: a.name }, a.name), project ? h("span", { class: "wproj", title: project }, project) : null),
    h("span", { class: "wtime" + (cooling ? "" : " ready"), ...(cooling ? { "data-expiry": a.ts_expired, role: "timer" } : {}) }, cooling ? "…" : "Ready"),
    h("div", { class: "wact" },
      cooling ? btn("extend", ext, "", () => start(a)) : btn("play", "Start cooldown for " + a.name, "go", () => start(a)),
      !cooling && a.has_prompt ? btn("copy", "Copy prompt for " + a.name, "", () => copy(a)) : null));
}
function render() {
  mount(root, W.accounts.length ? W.accounts.map(row) : h("p", { class: "msg" }, "No accounts shown."));
  tick();
}
function tick() {
  const now = nowMs(); let lapsed = false;
  document.querySelectorAll("[data-expiry]").forEach(el => { const left = Date.parse(el.dataset.expiry) - now; if (left <= 0) { lapsed = true; el.textContent = "Ready"; } else el.textContent = fmtRemaining(left); });
  if (lapsed && !tick.pending) { tick.pending = true; setTimeout(() => { tick.pending = false; refresh(); }, 1500); }
}
async function refresh() {
  try {
    const d = await call("GET", "/summary");
    W.accounts = d.accounts; W.opts = d; W.skew = Date.parse(d.now) - Date.now();
    render(); clearInterval(timer); timer = setInterval(tick, 1000);
    clearInterval(poll); poll = setInterval(() => { if (!document.hidden) refresh(); }, 60000);
  } catch (e) { say(e.message, true); }
}
async function start(a) {  // Start (available) or Extend by the default duration (cooling): direct, no dialog
  W.busy.add(a.id); say(""); render();
  let err = "";
  try { await call("POST", `/accounts/${encodeURIComponent(a.id)}/start`); } catch (e) { err = `${a.name}: ${e.message}`; }
  W.busy.delete(a.id); await refresh(); if (err) say(err, true);
}
async function copy(a) {
  W.busy.add(a.id); say(""); render();
  try {
    const { prompt } = await call("GET", `/accounts/${encodeURIComponent(a.id)}/prompt`);
    if (await writeClipboard(prompt)) say("Copied");
    else {  // blocked (e.g. no allow="clipboard-write", or plain HTTP): let the person copy by hand
      const t = h("textarea", { readonly: true, "aria-label": "Prompt text" }); t.value = prompt; setTimeout(() => t.select(), 0);
      say("Your browser blocked clipboard access in this frame (the embedding page needs allow=\"clipboard-write\"). Copy the text below.", false, t, h("button", { type: "button", onclick: () => say("") }, "Close"));
    }
  } catch (e) { say(`${a.name}: ${e.message}`, true); }
  W.busy.delete(a.id); render();
}
async function writeClipboard(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through to the legacy path */ }
  try {
    const ta = h("textarea", { "aria-hidden": "true" }); ta.value = text; document.body.append(ta); ta.select();
    const ok = document.execCommand("copy"); ta.remove(); return !!ok;
  } catch { return false; }
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); refresh(); } });
refresh();
