"use strict";
const { h, mount, fmtRemaining, iconEl } = RS;
const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const $ = (s, r = document) => r.querySelector(s);
const S = { accounts: [], config: null, placeholders: [], analyticsError: null, skew: 0, baseUrl: "" };
const MAX_IMAGE = 512 * 1024, IMAGE_TYPES = ["image/svg+xml", "image/png", "image/webp", "image/jpeg"];
let tickTimer = null, refetchScheduled = false;

async function request(method, path, body, raw) {
  const headers = { "X-CSRF-Token": CSRF };
  if (raw) headers["Content-Type"] = raw.type || "application/octet-stream";
  else if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch("/api" + path, { method, headers, body: raw || (body === undefined ? undefined : JSON.stringify(body)), credentials: "same-origin" });
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : null;
  if (!res.ok) { const e = new Error(typeof data?.detail === "string" ? data.detail : data?.detail?.[0]?.msg || `HTTP ${res.status}`); e.code = data?.code; throw e; }
  return data;
}
const api = (method, path, body) => request(method, path, body);
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 3500); }
const nowMs = () => Date.now() + S.skew;
const tzOpts = () => ({ timeZone: S.config.timezone, dateStyle: "medium", timeStyle: "short" });
function fmtLocal(iso) { return new Date(iso).toLocaleString(undefined, tzOpts()); }
/* "6:30 PM" today, "Fri, 6:30 PM" on another day, always in the application's effective time zone. */
function fmtEnds(iso) {
  const tz = S.config.timezone, d = new Date(iso);
  try {
    const day = x => new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(x);
    return day(d) === day(new Date(nowMs())) ? d.toLocaleTimeString(undefined, { timeZone: tz, hour: "numeric", minute: "2-digit" })
      : d.toLocaleString(undefined, { timeZone: tz, weekday: "short", hour: "numeric", minute: "2-digit" });
  } catch { return d.toLocaleString(); }
}
/* Wall-clock "YYYY-MM-DDTHH:mm" in the effective time zone, as <input type=datetime-local> expects. */
function toLocalInput(iso) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: S.config.timezone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(iso));
  const g = t => parts.find(x => x.type === t).value;
  return `${g("year")}-${g("month")}-${g("day")}T${g("hour")}:${g("minute")}`;
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch {
    const ta = h("textarea", { "aria-hidden": "true" }); ta.value = text; document.body.append(ta); ta.select();
    let ok = false; try { ok = document.execCommand("copy"); } catch { /* unsupported */ }
    ta.remove(); toast(ok ? "Copied" : "Copy is blocked by the browser — select the text manually");
  }
}
async function load() {
  const d = await api("GET", "/bootstrap");
  S.accounts = d.accounts; S.config = d.config; S.placeholders = d.placeholders; S.analyticsError = d.analytics_error; S.baseUrl = d.public_base_url || "";
  S.skew = Date.parse(d.now) - Date.now();
}
async function act(fn, okMsg) {
  try { const r = await fn(); if (okMsg) toast(okMsg); if (r?.warnings?.length) toast(r.warnings.join(" ")); await load(); route(); return r; }
  catch (e) { toast(e.message); }
}

/* ------------------------------------------------------------ countdown */
function tick() {
  const now = nowMs(); let expired = false;
  document.querySelectorAll("[data-expiry]").forEach(el => {
    const exp = Date.parse(el.dataset.expiry), start = Date.parse(el.dataset.start), left = exp - now;
    const card = el.closest(".card");
    if (left <= 0) { expired = true; el.textContent = "Available"; }
    else { el.textContent = fmtRemaining(left); }
    const bar = card.querySelector(".bar2 i"); if (bar) bar.style.width = left <= 0 ? "100%" : `${Math.min(100, Math.max(0, (now - start) / (exp - start) * 100))}%`;
  });
  if (expired && !refetchScheduled) { refetchScheduled = true; setTimeout(async () => { refetchScheduled = false; await load(); route(); }, 1500); }
}
function startTicker() { clearInterval(tickTimer); tick(); tickTimer = setInterval(tick, 1000); }
document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); load().then(() => { if (location.hash === "" || location.hash === "#/") route(); }).catch(() => {}); } });

/* ------------------------------------------------------------ views */
/* Start cooldown dialog (main site only; the widget starts/extends directly). */
function cooldownDialog(a) {
  const dlg = $("#dlg"), err = h("p", { class: "err", role: "alert" }), cooling = a.status === "cooling";
  let mode = "default", busy = false;
  const curProject = (a.project || "").trim(), curPrompt = (a.prompt || "").trim();
  const dur = RS.fmtDuration(a.duration_seconds);
  const choices = [
    ["default", cooling ? "Extend by default" : "Use default duration", cooling ? `Adds ${dur} to the current end time` : `${dur} from now`],
    ["at", "Set completion time", "Pick the exact end date and time"],
    ["remind", "Remind me at", "One-off Slack reminder; the cooldown is not changed"],
  ];
  const atInput = h("input", { type: "datetime-local", name: "completes_at", step: 60, "aria-label": "Cooldown completes at" });
  const occ = h("select", { name: "occurrence", "aria-label": "Which occurrence" }, h("option", { value: "earlier" }, "Earlier occurrence"), h("option", { value: "later" }, "Later occurrence"));
  const occLabel = h("label", { hidden: true }, "That time occurs twice. Which one?", occ);
  const atBox = h("div", { class: "modebox", hidden: true }, h("label", {}, "Completes at", atInput), occLabel,
    h("span", { class: "hint" }, `Time zone: ${S.config.timezone}. The default duration is not changed.`));
  const hhmm = h("input", { type: "text", name: "time", inputmode: "numeric", placeholder: "HH:MM", maxlength: 5, autocomplete: "off", "aria-label": "Reminder time (HH:MM)" });
  const remBox = h("div", { class: "modebox", hidden: true }, h("label", {}, "Remind at (HH:MM, 24-hour)", hhmm),
    h("span", { class: "hint" }, `Time zone: ${S.config.timezone}. If that time has passed today, the reminder is set for tomorrow. Requires Slack notifications.`));
  const project = h("input", { name: "project", maxlength: 100, placeholder: curProject || "No project saved", autocomplete: "off" });
  const prompt = h("textarea", { name: "prompt", maxlength: 4000, placeholder: curPrompt ? curPrompt.slice(0, 160) + (curPrompt.length > 160 ? "…" : "") : "No continuation prompt saved" });
  const metaBox = h("div", { class: "modebox" },
    h("label", {}, "Project", project, h("span", { class: "hint" }, curProject ? `Leave empty to keep “${curProject}”.` : "Leave empty to keep it unset.")),
    h("label", {}, "Continuation prompt", prompt, h("span", { class: "hint" }, curPrompt ? "Leave empty to keep the saved prompt." : "Leave empty to keep it unset.")));
  const submit = h("button", { class: "primary grow", type: "submit" }, "Start cooldown");
  const radios = choices.map(([m, title, sub]) => {
    const r = h("input", { type: "radio", name: "mode", value: m, checked: m === mode, onchange: () => setMode(m) });
    return h("label", { class: "opt" }, r, h("span", {}, h("strong", {}, title), h("small", {}, sub)));
  });
  function setMode(m) {
    mode = m; err.textContent = "";
    atBox.hidden = m !== "at"; remBox.hidden = m !== "remind";
    submit.textContent = m === "remind" ? "Set reminder" : cooling && m === "default" ? "Extend cooldown" : "Start cooldown";
  }
  setMode("default");
  const f = h("form", { novalidate: true, onsubmit: async ev => {
    ev.preventDefault(); if (busy) return;
    err.textContent = "";
    if (mode === "at" && !atInput.value) { err.textContent = "Choose the date and time the cooldown should finish."; return; }
    if (mode === "remind" && !/^([01]?\d|2[0-3]):[0-5]\d$/.test(hhmm.value.trim())) { err.textContent = "Enter the reminder time as HH:MM (24-hour), e.g. 18:30."; return; }
    busy = true; submit.disabled = true; submit.setAttribute("aria-busy", "true");
    try {
      let r;
      if (mode === "remind") {
        r = await api("POST", `/accounts/${a.id}/reminder`, { time: hhmm.value.trim(), project: project.value, prompt: prompt.value });
        dlg.close(); toast("Reminder set for " + fmtLocal(r.remind_at));
      } else {
        r = await api("POST", `/accounts/${a.id}/cooldown`, { mode, project: project.value, prompt: prompt.value,
          ...(mode === "at" ? { completes_at: atInput.value, ...(occLabel.hidden ? {} : { occurrence: occ.value }) } : {}) });
        dlg.close(); toast(r?.warnings?.length ? r.warnings.join(" ") : r.event === "extend" ? "Cooldown extended" : "Cooldown started");
      }
      await load(); route();
    } catch (e) {
      if (e.code === "ambiguous_time") occLabel.hidden = false;
      err.textContent = e.message;
      if (e.code === "partial") { await load(); route(); }  // the metadata part was saved: show it behind the dialog
      busy = false; submit.disabled = false; submit.removeAttribute("aria-busy");
    }
  } },
    h("h2", {}, a.name), h("div", { class: "opts", role: "radiogroup", "aria-label": "Cooldown action" }, radios),
    atBox, remBox, metaBox, err,
    h("div", { class: "row" }, submit, h("button", { type: "button", onclick: () => dlg.close() }, "Cancel")));
  mount(dlg, f); dlg.showModal();
}
function card(a) {
  const cooling = a.status === "cooling", project = (a.project || "").trim();
  return h("article", { class: "card " + (cooling ? "cooling" : "available"), "aria-label": a.name },
    h("div", { class: "top" }, iconEl(a),
      h("div", { class: "who" }, h("h3", { title: a.name }, a.name), project ? h("p", { class: "proj", title: project }, project) : null),
      h("div", { class: "acts" },
        RS.ibtn("edit", "Edit " + a.name, { onclick: () => accountDialog(a) }),
        cooling ? null : RS.ibtn("copy", "Copy prompt for " + a.name, a.prompt ? { onclick: () => copyText(a.prompt) } : { disabled: true, title: "No continuation prompt set" }))),
    cooling
      ? h("div", { class: "state" }, h("div", { class: "cd" }, h("span", { class: "label" }, "Cooling down"),
          h("span", { class: "time", "data-expiry": a.ts_expired, "data-start": a.ts_start, role: "timer", "aria-live": "off" }, "…"),
          h("span", { class: "until" }, "until " + fmtEnds(a.ts_expired))))
      : h("div", { class: "state" }, h("span", { class: "pill" }, "Available"),
          h("button", { class: "primary", onclick: () => cooldownDialog(a) }, RS.icon("play"), "Start cooldown")),
    cooling ? h("div", { class: "bar2", "aria-hidden": "true" }, h("i")) : null);
}
function groupSections(list) {
  if (!list.some(a => a.group)) return h("div", { class: "grid" }, list.map(card));  // no groups: unchanged layout
  const order = [], by = new Map();
  for (const a of list) { const g = a.group || ""; if (!by.has(g)) { by.set(g, []); if (g) order.push(g); } by.get(g).push(a); }
  if (by.has("")) order.push("");
  return order.map(g => h("section", { class: "group", "aria-label": g || "Ungrouped" },
    h("h2", { class: "grouptitle" }, g || "Ungrouped", h("span", { class: "count" }, String(by.get(g).length))),
    h("div", { class: "grid" }, by.get(g).map(card))));
}
function dashboard() {
  const list = S.accounts.filter(a => !a.archived);
  mount($("#main"), h("h2", { class: "sr" }, "Accounts"),
    S.analyticsError ? h("div", { class: "banner" }, S.analyticsError + ". Timers are unaffected; history may be incomplete.") : null,
    list.length ? groupSections(list) : h("div", { class: "emptycard" }, h("strong", {}, "No accounts yet"), "Use the + in the header to add your first account."));
  startTicker();
}
function archived() {
  const list = S.accounts.filter(a => a.archived);
  mount($("#main"), h("a", { class: "backlink", href: "#/settings" }, RS.icon("back"), "Settings"), h("h2", {}, "Archived accounts"), h("p", { class: "sub" }, "Archiving keeps analytics history. Restore an account to use it again."),
    list.length ? h("div", { class: "grid" }, list.map(a => h("article", { class: "card cooling", "aria-label": a.name }, h("div", { class: "top" }, iconEl(a), h("div", { class: "who" }, h("h3", { title: a.name }, a.name))),
      h("div", { class: "row" }, h("button", { onclick: () => act(() => api("POST", `/accounts/${a.id}/restore`), "Restored") }, "Restore"))))) : h("p", { class: "empty" }, "Nothing archived."));
}
async function uploadIcon(id, file) { return request("PUT", `/accounts/${id}/icon`, undefined, file); }
function accountDialog(a) {
  const dlg = $("#dlg"), err = h("p", { class: "err", role: "alert" });
  const hrs = a ? Math.floor(a.duration_seconds / 3600) : 5, mins = a ? Math.floor(a.duration_seconds % 3600 / 60) : 0;
  let pending = null, objectUrl = null, remove = false;
  const cooling = a?.status === "cooling";
  let expiryInit = cooling ? toLocalInput(a.ts_expired) : "";
  const expiry = cooling ? h("input", { type: "datetime-local", name: "completes_at", value: expiryInit, step: 60, "aria-describedby": "exphint" }) : null;
  const occ = cooling ? h("select", { name: "occurrence" }, h("option", { value: "earlier" }, "Earlier occurrence"), h("option", { value: "later" }, "Later occurrence")) : null;
  const occLabel = cooling ? h("label", { hidden: true }, "That time occurs twice. Which one?", occ) : null;
  const emoji = h("input", { name: "icon", maxlength: 8, value: a?.icon || "⏳" });
  const preview = h("span", { class: "preview" });
  const drawPreview = () => {
    if (objectUrl) URL.revokeObjectURL(objectUrl), objectUrl = null;
    if (pending) { objectUrl = URL.createObjectURL(pending); mount(preview, h("img", { class: "icon", src: objectUrl, alt: "Preview of the selected image", width: 48, height: 48 })); }
    else if (a?.icon_url && !remove) mount(preview, iconEl(a));
    else mount(preview, h("span", { class: "icon", role: "img", "aria-label": "Default icon" }, emoji.value || "⏳"));
  };
  emoji.addEventListener("input", () => { if (!pending && !(a?.icon_url && !remove)) drawPreview(); });
  const file = h("input", { type: "file", name: "image", accept: IMAGE_TYPES.join(","), "aria-describedby": "imghint", onchange: () => {
    err.textContent = ""; const f = file.files[0]; if (!f) return;
    if (f.size > MAX_IMAGE) { err.textContent = "That image is larger than 512 KB."; file.value = ""; return; }
    if (f.type && !IMAGE_TYPES.includes(f.type)) { err.textContent = "Use an SVG, PNG, WebP or JPEG image."; file.value = ""; return; }
    pending = f; remove = false; drawPreview(); rm.disabled = false;
  } });
  const rm = h("button", { type: "button", disabled: !(a?.icon_url), onclick: () => { pending = null; file.value = ""; remove = true; rm.disabled = true; drawPreview(); } }, "Remove image");
  drawPreview();
  const f = h("form", { novalidate: true, onsubmit: async ev => {
    ev.preventDefault(); err.textContent = "";
    const v = Object.fromEntries(new FormData(f));
    const body = { name: v.name, icon: v.icon || "⏳", duration_seconds: (+v.hours || 0) * 3600 + (+v.minutes || 0) * 60, project: v.project, prompt: v.prompt, group: v.group || "" };
    const moveExpiry = cooling && expiry.value !== expiryInit;  // untouched field => the running timer is never rewritten
    if (moveExpiry && !expiry.value) { err.textContent = "Enter the cooldown completion date and time."; return; }
    let saved;
    try { saved = await (a ? api("PUT", `/accounts/${a.id}`, body) : api("POST", "/accounts", body)); }
    catch (e) { err.textContent = e.message; return; }
    const notes = [];
    try { if (pending) { await uploadIcon(saved.id, pending); pending = null; } else if (remove && a?.icon_url) { await api("DELETE", `/accounts/${saved.id}/icon`); remove = false; } }
    catch (e) { notes.push("the image was not changed: " + e.message); }
    if (moveExpiry) {  // separate backend operation: report a partial failure honestly and allow a retry
      try {
        await api("PUT", `/accounts/${saved.id}/expiry`, { completes_at: expiry.value, ...(occLabel.hidden ? {} : { occurrence: occ.value }) });
        expiryInit = expiry.value;
      } catch (e) {
        if (e.code === "ambiguous_time") occLabel.hidden = false;
        await load(); route();  // show what is actually persisted behind the dialog
        err.textContent = "Your other changes were saved, but the completion time was not changed: " + e.message + (notes.length ? " (Also " + notes.join("; ") + ".)" : "") + " Fix it and press Save to retry.";
        return;
      }
    }
    dlg.close(); toast(notes.length ? "Saved, but " + notes.join("; ") : "Saved"); await load(); route();
  } },
    h("h2", {}, a ? "Edit account" : "New account"),
    h("label", {}, "Name", h("input", { name: "name", required: true, maxlength: 60, value: a?.name || "", placeholder: "ChatGPT — Work" })),
    h("fieldset", { class: "imgset" }, h("legend", {}, "Image"),
      h("div", { class: "imgrow" }, preview, h("div", { class: "imgctl" }, file, rm)),
      h("span", { id: "imghint", class: "hint" }, "SVG, PNG, WebP or JPEG, up to 512 KB. SVGs are sanitised on upload. Without an image the emoji below is shown."),
      h("label", {}, "Fallback emoji (also used as $icon in Slack)", emoji)),
    cooling ? h("label", {}, "Cooldown completes at", expiry, occLabel,
      h("span", { id: "exphint", class: "hint" }, `Time zone: ${S.config.timezone}. Moves the end of the running cooldown only; the default duration below is not changed.`)) : null,
    h("div", { class: "two" }, h("label", {}, "Cooldown hours", h("input", { name: "hours", type: "number", min: 0, max: 720, value: hrs, inputmode: "numeric" })),
      h("label", {}, "Minutes", h("input", { name: "minutes", type: "number", min: 0, max: 59, value: mins, inputmode: "numeric" }))),
    h("p", { class: "hint" }, "The default duration is what Start cooldown uses. Changing it never moves a running cooldown."),
    h("label", {}, "Group", h("input", { name: "group", maxlength: 40, value: a?.group || "", list: "grouplist", autocomplete: "off", placeholder: "Optional, e.g. Work" }),
      h("datalist", { id: "grouplist" }, [...new Set(S.accounts.map(x => x.group).filter(Boolean))].sort().map(g => h("option", { value: g }))),
      h("span", { class: "hint" }, "Accounts with the same group appear together in their own section on the dashboard. Leave empty for none.")),
    h("label", {}, "Current project", h("input", { name: "project", maxlength: 100, value: a?.project || "" }),
      h("span", { class: "hint" }, "Changing the project or prompt never touches the running cooldown.")),
    h("label", {}, "Current continuation prompt", h("textarea", { name: "prompt", maxlength: 4000 }, a?.prompt || ""),
      h("span", { class: "hint" }, "The full prompt is sent to Slack when the cooldown ends.")),
    err,
    h("div", { class: "row" }, h("button", { class: "primary grow", type: "submit" }, "Save"), h("button", { type: "button", onclick: () => dlg.close() }, "Cancel"),
      a ? h("button", { type: "button", class: "danger", onclick: async () => { if (confirm(`Archive "${a.name}"? History is kept and you can restore it later.`)) { dlg.close(); act(() => api("POST", `/accounts/${a.id}/archive`), "Archived"); } } }, "Archive") : null));
  dlg.addEventListener("close", () => { if (objectUrl) URL.revokeObjectURL(objectUrl); }, { once: true });
  mount(dlg, f); dlg.showModal();
}

/* ------------------------------------------------------------ statistics (graphs) */
const hours = s => Math.round(s / 36) / 100;  // seconds -> hours, 2 dp
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function chartCard(title, summary, spec, legendFrom) {
  const host = h("div", { class: "plot" });
  const sec = h("section", { class: "panel chart" }, h("h3", {}, title), h("p", { class: "sub summary" }, summary),
    h("div", { role: "img", "aria-label": `${title}. ${summary}` }, host),
    h("ul", { class: "legend", "aria-hidden": "true" }, (legendFrom || spec.series).map((s, i) => h("li", {}, h("i", { class: "sw c" + ((i % 4) + 1) }), s.name))));
  requestAnimationFrame(() => RS.charts.chart(host, spec));
  return sec;
}
const emptyChart = (title, why) => h("section", { class: "panel chart" }, h("h3", {}, title), h("p", { class: "empty" }, why));
const sum = a => a.reduce((x, y) => x + y, 0);
function renderCharts(out, d) {
  RS.charts.clear();
  if (!d.has_history) return mount(out, h("div", { class: "panel empty" }, h("h3", {}, "No history yet"), h("p", {}, "Events are recorded from the moment statistics are enabled. Start or extend a cooldown and the charts will appear here.")));
  if (!d.matching_events) return mount(out, h("div", { class: "panel empty" }, h("h3", {}, "No events match these filters"), h("p", {}, "Widen the date range or clear the account/project filter.")));
  const A = d.activity, B = d.by_account, D = d.duration_by_account, P = d.by_project, M = d.monthly, R = d.reset_hours;
  const grp = { day: "Day", week: "Week starting (Mon)", month: "Month" }[d.filters.group];
  const names = { start: "Starts", extend: "Extensions", complete: "Completions" };
  const ser = (o, keys) => keys.map(k => ({ name: names[k] || k, data: o[k] }));
  const evs = ["start", "extend", "complete"];
  const cards = [
    chartCard("Cooldown activity over time", `${plural(sum(A.start), "start")}, ${plural(sum(A.extend), "extension")} and ${plural(sum(A.complete), "completion")} across ${plural(A.buckets.length, d.filters.group)} with activity.`,
      { mode: "line", labels: A.buckets, series: ser(A, evs), xTitle: grp, yTitle: "Events", integer: true }),
    chartCard("Activity by account", `Event counts for ${plural(B.labels.length, "account")}.`,
      { mode: "stack", labels: B.labels, series: ser(B, evs), xTitle: "Account", yTitle: "Events", integer: true }),
    D.labels.length ? chartCard("Completed cooldown time by account", `Configured cooldown hours that ran to completion, across ${plural(D.labels.length, "account")}. This is timer length, not measured service usage.`,
      { mode: "bar", labels: D.labels, series: [{ name: "Configured hours", data: D.seconds.map(hours) }], xTitle: "Account", yTitle: "Hours (configured)", tipFmt: (v, i) => `${v} h over ${plural(D.completions[i], "completion")}` }, [{ name: "Configured hours" }])
      : emptyChart("Completed cooldown time by account", "No cooldown has completed in this range yet."),
    chartCard("Activity by project", `Starts and extensions per project snapshot, across ${plural(P.labels.length, "project")}. Project names are snapshots from when each event happened.`,
      { mode: "stack", labels: P.labels, series: [{ name: "Starts", data: P.starts }, { name: "Extensions", data: P.extends }], xTitle: "Project", yTitle: "Events", integer: true }),
    M.buckets.length ? chartCard("Monthly trend", `Completed configured cooldown hours per month, across ${plural(M.buckets.length, "month")}.`,
      { mode: "line", labels: M.buckets, series: [{ name: "Configured hours", data: M.seconds.map(hours) }], xTitle: "Month", yTitle: "Hours (configured)", tipFmt: (v, i) => `${v} h over ${plural(M.completions[i], "completion")}` })
      : emptyChart("Monthly trend", "No cooldown has completed in this range yet."),
    chartCard("Reset activity by hour of day", `When cooldowns are started or extended, by hour in ${d.timezone}. ${plural(sum(R.start) + sum(R.extend), "reset")} in range.`,
      { mode: "stack", labels: R.hours.map(x => String(x).padStart(2, "0") + ":00"), series: [{ name: "Starts", data: R.start }, { name: "Extensions", data: R.extend }], xTitle: `Hour (${d.timezone})`, yTitle: "Resets", integer: true }),
  ];
  mount(out, h("div", { class: "chartgrid" }, cards));
}
async function stats() {
  const main = $("#main");
  mount(main, h("h2", {}, "Statistics"), h("p", { class: "sub" }, "Durations are configured cooldown lengths, not measured service usage."));
  if (!S.config.statistics_enabled) { main.append(h("div", { class: "panel empty" }, h("h3", {}, "Statistics are off"), h("p", {}, "No history database exists. Enable statistics under Settings to start collecting history; earlier activity is not reconstructed."), h("a", { class: "btn", href: "#/settings" }, "Open settings"))); return; }
  const q = { from: "", to: "", group: "day", account: "", project: "" }, out = h("div", { class: "charts", "aria-live": "polite" }), bar = h("form", { class: "filters", novalidate: true, "aria-label": "Chart filters", onsubmit: e => e.preventDefault() });
  const fetchGraphs = async () => {
    const p = new URLSearchParams(); if (q.from) p.set("from", q.from); if (q.to) p.set("to", q.to); p.set("group", q.group); if (q.account) p.set("account", q.account); if (q.project) p.set("project", q.project);
    return api("GET", "/stats/graphs?" + p);
  };
  const refresh = async () => { try { renderCharts(out, await fetchGraphs()); } catch (e) { mount(out, h("p", { class: "err", role: "alert" }, e.message)); } };
  const sel = (label, key, opts) => h("label", {}, label, h("select", { name: key, onchange: ev => { q[key] = ev.target.value; refresh(); } }, opts.map(([v, t]) => h("option", { value: v, selected: q[key] === v }, t))));
  const date = (label, key) => h("label", {}, label, h("input", { type: "date", name: key, value: q[key], onchange: ev => { q[key] = ev.target.value; refresh(); } }));
  mount(main, h("h2", {}, "Statistics"), h("p", { class: "sub" }, "Durations are configured cooldown lengths, not measured service usage."), bar, out);
  try {
    const d = await fetchGraphs();
    mount(bar, date("From", "from"), date("To", "to"),
      sel("Group by", "group", [["day", "Day"], ["week", "Week"], ["month", "Month"]]),
      sel("Account", "account", [["", "All accounts"], ...d.options.accounts.map(a => [a.id, a.name])]),
      sel("Project", "project", [["", "All projects"], ...d.options.projects.map(p => [p, p])]));
    renderCharts(out, d);
  } catch (e) { mount(out, h("p", { class: "err", role: "alert" }, e.message)); }
}

/* ------------------------------------------------------------ settings */
function field(label, input, hint) { return h("label", {}, label, input, hint ? h("span", { class: "hint" }, hint) : null); }
function panel(title, ...kids) { return h("section", { class: "panel" }, h("h3", {}, title), ...kids); }
function saveForm(title, build, collect) {
  const msg = h("p", { role: "status" });
  const f = h("form", { novalidate: true, onsubmit: async ev => { ev.preventDefault(); msg.className = ""; msg.textContent = "";
    try { S.config = await api("PUT", "/settings", collect(f)); msg.className = "okmsg"; msg.textContent = "Saved"; applyTheme(); } catch (e) { msg.className = "err"; msg.textContent = e.message; } } },
    ...build(), msg, h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save")));
  return panel(title, f);
}
function applyTheme() { document.documentElement.dataset.theme = S.config.theme; RS.charts.restyle(); }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => RS.charts.restyle());
async function settings() {
  const c = S.config, main = $("#main"), num = (n, v, lo, hi) => h("input", { name: n, type: "number", min: lo, max: hi, value: v, inputmode: "numeric" });
  const tpl = h("textarea", { class: "code", name: "template", spellcheck: false, "aria-label": "Block Kit JSON template" }, ""); tpl.value = c.slack.template || "";
  const out = h("pre", { "aria-live": "polite", hidden: true }), status = h("p", { role: "status" });
  const ENV_NOTE = "Already set in environment variable", envTz = !!c.env?.timezone, envHook = c.slack.webhook_source === "environment";
  const hook = envHook ? h("input", { name: "webhook_url", type: "text", disabled: true, value: ENV_NOTE, "aria-label": "Webhook URL (managed by SLACK_WEBHOOK_URL)" })
    : h("input", { name: "webhook_url", type: "password", autocomplete: "off", placeholder: c.slack.webhook_configured ? "Saved: " + c.slack.webhook_masked + " (leave blank to keep)" : "https://hooks.slack.com/services/…" });
  const slackEnabled = h("input", { type: "checkbox", name: "enabled", checked: c.slack.enabled });
  const themeSel = h("select", { name: "theme" }, ["system", "light", "dark"].map(t => h("option", { value: t, selected: c.theme === t }, t[0].toUpperCase() + t.slice(1))));
  mount(main, h("h2", {}, "Settings"), h("p", { class: "sub" }, "Everything is stored in encrypted files; nothing here requires editing JSON by hand."),
    saveForm("General & theme", () => [field("Theme", themeSel, "System follows your OS and updates live."),
      field("Time zone (IANA name)", envTz ? h("input", { name: "timezone", type: "text", disabled: true, value: ENV_NOTE }) : h("input", { name: "timezone", value: c.timezone }),
        envTz ? `Managed by the READY_SIGNAL_TIMEZONE environment variable (in effect: ${c.timezone}). Change it in the deployment environment and recreate the container.` : "e.g. Asia/Kolkata, Europe/Berlin, UTC"),
      field("Expiry reconciliation interval (s)", num("expiry_check_interval", c.expiry_check_interval, 5, 3600), "Safety net only; the scheduler wakes at exact deadlines.")],
      f => ({ theme: f.elements.theme.value, ...(envTz ? {} : { timezone: f.elements.timezone.value }), expiry_check_interval: +f.elements.expiry_check_interval.value })),
    panel("Accounts", h("p", { class: "sub" }, "Archived accounts keep their analytics history and can be restored."), h("a", { class: "btn", href: "#/archived" }, RS.icon("archive"), "Archived accounts")),
    saveForm("Timestamp formats", () => [field("$ts_start format", h("input", { name: "a", value: c.ts_start_format }), "Python strftime, e.g. %Y-%m-%d %H:%M:%S %Z"),
      field("$ts_expired format", h("input", { name: "b", value: c.ts_expired_format }))], f => ({ ts_start_format: f.elements.a.value, ts_expired_format: f.elements.b.value })),
    saveForm("Delivery & retries", () => [h("div", { class: "two" }, field("Retry interval (s)", num("retry", c.retry_interval, 1, 86400)), field("Max attempts", num("max", c.max_attempts, 1, 50))),
      h("div", { class: "two" }, field("Connect timeout (s)", num("ct", c.connect_timeout, 1, 60)), field("Request timeout (s)", num("rt", c.request_timeout, 2, 120)))],
      f => ({ retry_interval: +f.elements.retry.value, max_attempts: +f.elements.max.value, connect_timeout: +f.elements.ct.value, request_timeout: +f.elements.rt.value })),
    panel("Slack webhook & Block Kit template",
      h("p", { class: "sub" }, "The full continuation prompt and project name are sent to Slack — an external service. Incoming webhooks post to their own configured channel."),
      h("form", { novalidate: true, onsubmit: async ev => { ev.preventDefault(); status.className = ""; status.textContent = "";
        try { S.config = await api("PUT", "/settings", { slack: { enabled: slackEnabled.checked, ...(envHook ? {} : { webhook_url: hook.value }), template: tpl.value } }); if (!envHook) { hook.value = ""; hook.placeholder = "Saved: " + S.config.slack.webhook_masked + " (leave blank to keep)"; } status.className = "okmsg"; status.textContent = "Saved"; }
        catch (e) { status.className = "err"; status.textContent = e.message; } } },
        h("label", { class: "check" }, slackEnabled, "Send Slack notifications"), field("Webhook URL", hook, envHook ? "Managed by the SLACK_WEBHOOK_URL environment variable; the URL is never displayed. Change it in the deployment environment and recreate the container." : "Stored encrypted; never shown again after saving."),
        field("Message template (JSON with fallback text and blocks)", tpl),
        h("p", { class: "hint" }, "Placeholders: ", S.placeholders.map(p => h("code", {}, "$" + p)).flatMap(x => [x, " "]), " Use ", h("code", {}, "$$"), " for a literal dollar. Unknown placeholders stay as written."),
        status, out,
        h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save"),
          h("button", { type: "button", onclick: async () => { try { const p = await api("POST", "/settings/template/preview", { template: tpl.value }); out.hidden = false; out.className = ""; out.textContent = JSON.stringify(p, null, 2); status.textContent = ""; } catch (e) { out.hidden = true; status.className = "err"; status.textContent = e.message; } } }, "Validate & preview"),
          h("button", { type: "button", onclick: async () => { status.className = ""; status.textContent = "Sending…"; try { const r = await api("POST", "/settings/slack/test"); status.className = r.ok ? "okmsg" : "err"; status.textContent = r.ok ? "Test message delivered" : "Failed: " + r.error; } catch (e) { status.className = "err"; status.textContent = e.message; } } }, "Send test"),
          c.slack.webhook_configured && !envHook ? h("button", { type: "button", class: "danger", onclick: async () => { if (confirm("Remove the saved webhook URL?")) { S.config = await api("PUT", "/settings", { slack: { enabled: false, clear_webhook: true } }); settings(); } } }, "Remove webhook") : null))),
    widgetPanel(), deliveryPanel(), statsPanel(), dataPanel());
}
const WIDGET_ROW = 34, WIDGET_PAD = 12;  // must match widget.css (--row, container padding)
function widgetPanel() {
  const w = S.config.widget, msg = h("p", { role: "status" }), active = S.accounts.filter(a => !a.archived);
  const enabled = h("input", { type: "checkbox", name: "enabled", checked: w.enabled });
  const showProject = h("input", { type: "checkbox", checked: w.show_project }), allowPrompt = h("input", { type: "checkbox", checked: w.allow_prompt_copy });
  const snippetEl = h("pre"), base = S.baseUrl || location.origin;
  const draw = () => {
    const n = picks.filter(p => p.box.checked).length || picks.length, height = Math.max(150, WIDGET_PAD + WIDGET_ROW * n);
    snippetEl.textContent = `<iframe src="${base}/widget" title="Ready Signal" allow="clipboard-write" loading="lazy" width="280" height="${height}" style="border:0"></iframe>`;
  };
  const picks = active.map(a => ({ a, box: h("input", { type: "checkbox", checked: !w.account_ids.length || w.account_ids.includes(a.id), onchange: () => draw() }) }));
  draw();
  const form = h("form", { novalidate: true, onsubmit: async ev => { ev.preventDefault(); msg.className = ""; msg.textContent = "";
    const chosen = picks.filter(p => p.box.checked).map(p => p.a.id);
    try { S.config = await api("PUT", "/settings", { widget: { enabled: enabled.checked, show_project: showProject.checked, allow_prompt_copy: allowPrompt.checked, account_ids: chosen.length === picks.length ? [] : chosen } }); msg.className = "okmsg"; msg.textContent = "Saved"; }
    catch (e) { msg.className = "err"; msg.textContent = e.message; } } },
    h("label", { class: "check" }, enabled, "Enable the embeddable widget"),
    h("fieldset", { class: "imgset" }, h("legend", {}, "Accounts shown"), picks.length ? picks.map(p => h("label", { class: "check" }, p.box, p.a.name)) : h("p", { class: "hint" }, "No accounts yet.")),
    h("label", { class: "check" }, showProject, "Show the current project name"),
    h("label", { class: "check" }, allowPrompt, "Allow copying continuation prompts (the prompt text is served to the widget on demand)"),
    msg, h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save"), w.enabled ? h("a", { class: "btn", href: "/widget", target: "_blank", rel: "noopener" }, "Open widget") : null));
  return panel("Embeddable widget (iframe)",
    h("p", { class: "sub" }, "A compact cooldown view (about 280 × 150 px) for a dashboard on your private network. It needs no credential: anyone who can reach this address can use it, so keep Ready Signal off the public internet."),
    form, h("h4", {}, "Embed code"), snippetEl,
    h("p", { class: "hint" }, "The height follows the accounts shown (34 px per account). Keep allow=\"clipboard-write\" so Copy prompt works inside the frame. Set PUBLIC_BASE_URL on the server so this snippet shows your real address."));
}
function deliveryPanel() {
  const body = h("tbody"), p = panel("Delivery status", h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Account", "Status", "Tries", "HTTP", "Error", ""].map(x => h("th", {}, x)))), body)));
  api("GET", "/notifications").then(d => { if (!d.jobs.length) body.append(h("tr", {}, h("td", { colspan: 6 }, "No notifications yet."))); d.jobs.slice(0, 20).forEach(j => body.append(h("tr", {}, h("td", {}, j.account), h("td", {}, j.status), h("td", {}, j.attempts), h("td", {}, j.last_http_status ?? "—"), h("td", {}, j.last_error || ""),
    h("td", {}, ["failed", "skipped"].includes(j.status) ? h("button", { onclick: () => act(() => api("POST", `/notifications/${j.id}/retry`), "Queued for retry").then(settings) }, "Retry") : null)))); });
  return p;
}
function statsPanel() {
  const on = S.config.statistics_enabled;
  return panel("Statistics (optional SQLite history)", h("p", {}, on ? "Statistics are ON." : "Statistics are OFF. No database exists."),
    h("p", { class: "hint" }, "Disabling permanently deletes the history database AND all project names and continuation prompts. Account names, images, durations, active timers and notification state are kept."),
    h("div", { class: "row" }, h("button", { class: on ? "danger" : "primary", onclick: async () => {
      if (on && !confirm("Delete all analytics history and every project name and continuation prompt? This cannot be undone.")) return;
      try { const r = await api("POST", "/settings/statistics", { enabled: !on, confirm: on }); if (r.errors.length) toast("Problem: " + r.errors.join("; ")); else toast(on ? "Statistics disabled and data deleted" : "Statistics enabled"); await load(); renderNav(); settings(); } catch (e) { toast(e.message); } } }, on ? "Disable & delete history" : "Enable statistics")));
}
function dataPanel() {
  const box = h("p", { class: "hint" }, "…");
  api("GET", "/encryption").then(d => { box.textContent = `${d.algorithm}, key derived with ${d.kdf.name} (N=${d.kdf.n}, r=${d.kdf.r}, p=${d.kdf.p}). SECRET comes from the environment and is never displayed. Files present: ` + Object.entries(d.files).filter(([, v]) => v).map(([k]) => k).join(", "); });
  return h("section", { class: "panel data", "aria-labelledby": "data-h" }, h("h3", { id: "data-h" }, "Data management"), h("h4", {}, "Encryption and recovery"),
    h("p", {}, "The backup is an archive of your accounts, timers, settings, notification queue and images exactly as stored on disk: every file stays AES-256-GCM encrypted and is unreadable without your SECRET."),
    box,
    h("div", { class: "actions" }, h("a", { class: "btn primary", href: "/api/backup", download: "ready-signal-backup-encrypted.tar.gz" }, RS.icon("download"), "Download encrypted backup")),
    h("p", { class: "hint" }, "Losing the SECRET makes the data unrecoverable. Restore and secret rotation are CLI procedures documented in docs/operations.md."));
}

/* ------------------------------------------------------------ routing */
function renderNav() {
  const cur = location.hash || "#/", here = href => (cur === href ? { "aria-current": "page" } : {});
  mount($("#nav"),
    S.config.statistics_enabled ? RS.ibtn("chart", "Statistics", { href: "#/stats", ...here("#/stats") }, "a") : null,
    RS.ibtn("plus", "Add account", { onclick: () => accountDialog() }),
    RS.ibtn("gear", "Settings", { href: "#/settings", ...(cur === "#/settings" || cur === "#/archived" ? { "aria-current": "page" } : {}) }, "a"));
  const brand = $(".brand"); if (cur === "#/") brand.setAttribute("aria-current", "page"); else brand.removeAttribute("aria-current");
}
function route() {
  clearInterval(tickTimer); RS.charts.clear(); document.documentElement.dataset.theme = S.config.theme; renderNav();
  const r = location.hash || "#/";
  ({ "#/": dashboard, "#/archived": archived, "#/stats": stats, "#/settings": settings }[r] || dashboard)();
}
window.addEventListener("hashchange", route);
load().then(route).catch(e => { $("#main").textContent = "Failed to load: " + e.message; });
