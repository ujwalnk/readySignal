"use strict";
const { h, mount, fmtRemaining, fmtDuration, iconEl } = RS;
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
  if (!res.ok) throw new Error(typeof data?.detail === "string" ? data.detail : data?.detail?.[0]?.msg || `HTTP ${res.status}`);
  return data;
}
const api = (method, path, body) => request(method, path, body);
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 3500); }
const nowMs = () => Date.now() + S.skew;
const tzOpts = () => ({ timeZone: S.config.timezone, dateStyle: "medium", timeStyle: "short" });
function fmtLocal(iso) { return new Date(iso).toLocaleString(undefined, tzOpts()); }

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
function card(a) {
  const cooling = a.status === "cooling";
  return h("article", { class: "card", "aria-label": a.name },
    h("header", {}, iconEl(a), h("h3", {}, a.name),
      h("span", { class: "chip " + (cooling ? "cool" : "ok") }, cooling ? "Cooling down" : "Available")),
    cooling ? h("div", { class: "time", "data-expiry": a.ts_expired, "data-start": a.ts_start, role: "timer", "aria-live": "off" }, "…")
            : h("div", { class: "time" }, "Ready"),
    h("div", { class: "bar2", "aria-hidden": "true" }, h("i")),
    h("div", { class: "meta" }, h("span", {}, "Duration " + fmtDuration(a.duration_seconds)),
      cooling ? h("span", {}, "Ends " + fmtLocal(a.ts_expired)) : null, h("span", {}, "Resets: " + a.reset_count)),
    h("p", { class: "project" + (a.project ? "" : " none") }, a.project ? h("span", {}, h("b", {}, "Project: "), a.project) : "No project set"),
    h("div", { class: "row" },
      h("button", { class: "primary grow", onclick: () => act(() => api("POST", `/accounts/${a.id}/start`), cooling ? "Cooldown extended" : "Cooldown started") }, cooling ? "Extend +" + fmtDuration(a.duration_seconds) : "Start cooldown"),
      h("button", { onclick: () => copyText(a.prompt), disabled: !a.prompt, "aria-label": "Copy continuation prompt for " + a.name }, "Copy prompt"),
      h("button", { onclick: () => accountDialog(a), "aria-label": "Edit " + a.name }, "Edit")));
}
function dashboard() {
  const list = S.accounts.filter(a => !a.archived);
  mount($("#main"),
    h("div", { class: "head" }, h("div", {}, h("h2", {}, "Accounts"), h("p", { class: "sub" }, "Timers are absolute timestamps — they survive restarts.")),
      h("button", { class: "primary", onclick: () => accountDialog() }, "+ New account")),
    S.analyticsError ? h("div", { class: "banner" }, S.analyticsError + ". Timers are unaffected; history may be incomplete.") : null,
    list.length ? h("div", { class: "grid" }, list.map(card)) : h("p", { class: "empty" }, "No accounts yet. Create one to start tracking a cooldown."));
  startTicker();
}
function archived() {
  const list = S.accounts.filter(a => a.archived);
  mount($("#main"), h("h2", {}, "Archived"), h("p", { class: "sub" }, "Archiving keeps analytics history."),
    list.length ? h("div", { class: "grid" }, list.map(a => h("article", { class: "card" }, h("header", {}, iconEl(a), h("h3", {}, a.name)),
      h("div", { class: "meta" }, "Duration " + fmtDuration(a.duration_seconds)),
      h("div", { class: "row" }, h("button", { onclick: () => act(() => api("POST", `/accounts/${a.id}/restore`), "Restored") }, "Restore"))))) : h("p", { class: "empty" }, "Nothing archived."));
}
async function uploadIcon(id, file) { return request("PUT", `/accounts/${id}/icon`, undefined, file); }
function accountDialog(a) {
  const dlg = $("#dlg"), err = h("p", { class: "err", role: "alert" });
  const hrs = a ? Math.floor(a.duration_seconds / 3600) : 5, mins = a ? Math.floor(a.duration_seconds % 3600 / 60) : 0;
  let pending = null, objectUrl = null, remove = false;
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
    const body = { name: v.name, icon: v.icon || "⏳", duration_seconds: (+v.hours || 0) * 3600 + (+v.minutes || 0) * 60, project: v.project, prompt: v.prompt };
    try {
      const saved = await (a ? api("PUT", `/accounts/${a.id}`, body) : api("POST", "/accounts", body));
      let note = "Saved";
      try { if (pending) await uploadIcon(saved.id, pending); else if (remove && a?.icon_url) await api("DELETE", `/accounts/${saved.id}/icon`); }
      catch (e) { note = "Saved, but the image was not changed: " + e.message; }
      dlg.close(); toast(note); await load(); route();
    } catch (e) { err.textContent = e.message; }
  } },
    h("h2", {}, a ? "Edit account" : "New account"),
    h("label", {}, "Name", h("input", { name: "name", required: true, maxlength: 60, value: a?.name || "", placeholder: "ChatGPT — Work" })),
    h("fieldset", { class: "imgset" }, h("legend", {}, "Image"),
      h("div", { class: "imgrow" }, preview, h("div", { class: "imgctl" }, file, rm)),
      h("span", { id: "imghint", class: "hint" }, "SVG, PNG, WebP or JPEG, up to 512 KB. SVGs are sanitised on upload. Without an image the emoji below is shown."),
      h("label", {}, "Fallback emoji (also used as $icon in Slack)", emoji)),
    h("div", { class: "two" }, h("label", {}, "Cooldown hours", h("input", { name: "hours", type: "number", min: 0, max: 720, value: hrs, inputmode: "numeric" })),
      h("label", {}, "Minutes", h("input", { name: "minutes", type: "number", min: 0, max: 59, value: mins, inputmode: "numeric" }))),
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
  const hook = h("input", { name: "webhook_url", type: "password", autocomplete: "off", placeholder: c.slack.webhook_configured ? "Saved: " + c.slack.webhook_masked + " (leave blank to keep)" : "https://hooks.slack.com/services/…" });
  const slackEnabled = h("input", { type: "checkbox", name: "enabled", checked: c.slack.enabled });
  const themeSel = h("select", { name: "theme" }, ["system", "light", "dark"].map(t => h("option", { value: t, selected: c.theme === t }, t[0].toUpperCase() + t.slice(1))));
  mount(main, h("h2", {}, "Settings"), h("p", { class: "sub" }, "Everything is stored in encrypted files; nothing here requires editing JSON by hand."),
    saveForm("General & theme", () => [field("Theme", themeSel, "System follows your OS and updates live."),
      field("Time zone (IANA name)", h("input", { name: "timezone", value: c.timezone }), "e.g. Asia/Kolkata, Europe/Berlin, UTC"),
      field("Expiry reconciliation interval (s)", num("expiry_check_interval", c.expiry_check_interval, 5, 3600), "Safety net only; the scheduler wakes at exact deadlines.")],
      f => ({ theme: f.theme.value, timezone: f.timezone.value, expiry_check_interval: +f.expiry_check_interval.value })),
    saveForm("Timestamp formats", () => [field("$ts_start format", h("input", { name: "a", value: c.ts_start_format }), "Python strftime, e.g. %Y-%m-%d %H:%M:%S %Z"),
      field("$ts_expired format", h("input", { name: "b", value: c.ts_expired_format }))], f => ({ ts_start_format: f.a.value, ts_expired_format: f.b.value })),
    saveForm("Delivery & retries", () => [h("div", { class: "two" }, field("Retry interval (s)", num("retry", c.retry_interval, 1, 86400)), field("Max attempts", num("max", c.max_attempts, 1, 50))),
      h("div", { class: "two" }, field("Connect timeout (s)", num("ct", c.connect_timeout, 1, 60)), field("Request timeout (s)", num("rt", c.request_timeout, 2, 120)))],
      f => ({ retry_interval: +f.retry.value, max_attempts: +f.max.value, connect_timeout: +f.ct.value, request_timeout: +f.rt.value })),
    panel("Slack webhook & Block Kit template",
      h("p", { class: "sub" }, "The full continuation prompt and project name are sent to Slack — an external service. Incoming webhooks post to their own configured channel."),
      h("form", { novalidate: true, onsubmit: async ev => { ev.preventDefault(); status.className = ""; status.textContent = "";
        try { S.config = await api("PUT", "/settings", { slack: { enabled: slackEnabled.checked, webhook_url: hook.value, template: tpl.value } }); hook.value = ""; hook.placeholder = "Saved: " + S.config.slack.webhook_masked + " (leave blank to keep)"; status.className = "okmsg"; status.textContent = "Saved"; }
        catch (e) { status.className = "err"; status.textContent = e.message; } } },
        h("label", { class: "check" }, slackEnabled, "Send Slack notifications"), field("Webhook URL", hook, "Stored encrypted; never shown again after saving."),
        field("Message template (JSON with fallback text and blocks)", tpl),
        h("p", { class: "hint" }, "Placeholders: ", S.placeholders.map(p => h("code", {}, "$" + p)).flatMap(x => [x, " "]), " Use ", h("code", {}, "$$"), " for a literal dollar. Unknown placeholders stay as written."),
        status, out,
        h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save"),
          h("button", { type: "button", onclick: async () => { try { const p = await api("POST", "/settings/template/preview", { template: tpl.value }); out.hidden = false; out.className = ""; out.textContent = JSON.stringify(p, null, 2); status.textContent = ""; } catch (e) { out.hidden = true; status.className = "err"; status.textContent = e.message; } } }, "Validate & preview"),
          h("button", { type: "button", onclick: async () => { status.className = ""; status.textContent = "Sending…"; try { const r = await api("POST", "/settings/slack/test"); status.className = r.ok ? "okmsg" : "err"; status.textContent = r.ok ? "Test message delivered" : "Failed: " + r.error; } catch (e) { status.className = "err"; status.textContent = e.message; } } }, "Send test"),
          c.slack.webhook_configured ? h("button", { type: "button", class: "danger", onclick: async () => { if (confirm("Remove the saved webhook URL?")) { S.config = await api("PUT", "/settings", { slack: { enabled: false, clear_webhook: true } }); settings(); } } }, "Remove webhook") : null))),
    widgetPanel(), deliveryPanel(), statsPanel(), encryptionPanel());
}
function widgetPanel() {
  const w = S.config.widget, msg = h("p", { role: "status" }), active = S.accounts.filter(a => !a.archived);
  const enabled = h("input", { type: "checkbox", name: "enabled", checked: w.enabled });
  const origin = h("input", { name: "origin", value: w.allowed_origin, placeholder: "https://dashboard.example.com", autocomplete: "off", inputmode: "url" });
  const showProject = h("input", { type: "checkbox", checked: w.show_project }), allowPrompt = h("input", { type: "checkbox", checked: w.allow_prompt_copy });
  const picks = active.map(a => ({ a, box: h("input", { type: "checkbox", checked: !w.account_ids.length || w.account_ids.includes(a.id) }) }));
  const tokenOut = h("div", { "aria-live": "polite" });
  const base = S.baseUrl || location.origin;
  const snippet = `<iframe src="${base}/widget" title="Ready Signal" allow="clipboard-write" loading="lazy" width="100%" height="320" style="border:0"></iframe>`;
  const form = h("form", { novalidate: true, onsubmit: async ev => { ev.preventDefault(); msg.className = ""; msg.textContent = "";
    const chosen = picks.filter(p => p.box.checked).map(p => p.a.id);
    try { S.config = await api("PUT", "/settings", { widget: { enabled: enabled.checked, allowed_origin: origin.value, show_project: showProject.checked, allow_prompt_copy: allowPrompt.checked, account_ids: chosen.length === picks.length ? [] : chosen } }); msg.className = "okmsg"; msg.textContent = "Saved"; }
    catch (e) { msg.className = "err"; msg.textContent = e.message; } } },
    h("label", { class: "check" }, enabled, "Enable the embeddable widget"),
    field("Authorised parent-dashboard origin", origin, "Exactly the origin of the page that embeds the iframe (scheme + host [+ port], no path). Only this origin may frame the widget."),
    h("fieldset", { class: "imgset" }, h("legend", {}, "Accounts shown"), picks.length ? picks.map(p => h("label", { class: "check" }, p.box, p.a.name)) : h("p", { class: "hint" }, "No accounts yet.")),
    h("label", { class: "check" }, showProject, "Show the current project name"),
    h("label", { class: "check" }, allowPrompt, "Allow copying continuation prompts (the prompt text is served to the widget on demand)"),
    msg, h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save")));
  const gen = h("button", { type: "button", onclick: async () => {
    if (w.token_configured && !confirm("Generate a new credential? The current one stops working immediately.")) return;
    try { const r = await api("POST", "/settings/widget/token"); S.config = (await api("GET", "/bootstrap")).config;
      const inp = h("input", { readonly: true, value: r.token, "aria-label": "New widget credential", onfocus: e => e.target.select() });
      mount(tokenOut, h("p", { class: "okmsg" }, "Copy this credential now — it is shown only once and is stored only as a hash."), inp, h("div", { class: "row" }, h("button", { type: "button", onclick: () => copyText(r.token) }, "Copy credential")));
    } catch (e) { toast(e.message); } } }, w.token_configured ? "Rotate credential" : "Generate credential");
  const revoke = w.token_configured ? h("button", { type: "button", class: "danger", onclick: async () => { if (confirm("Revoke the widget credential? Embedded widgets stop working until a new one is entered.")) { await api("DELETE", "/settings/widget/token"); await load(); settings(); } } }, "Revoke credential") : null;
  return panel("Embeddable widget (iframe)",
    h("p", { class: "sub" }, "A compact cooldown view for an existing dashboard. It needs a scoped credential (it can only list the chosen accounts, start/extend them and copy prompts). Never put the credential in a URL."),
    form,
    h("h4", {}, "Credential " + (w.token_configured ? "(configured)" : "(none yet)")),
    h("div", { class: "row" }, gen, revoke), tokenOut,
    h("h4", {}, "Embed code"), h("pre", {}, snippet),
    h("p", { class: "hint" }, "Paste the credential into the widget once when prompted, or have the parent page send it with postMessage — see the README. Set PUBLIC_BASE_URL on the server so this snippet shows your real address."));
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
function encryptionPanel() {
  const box = h("p", {}, "…"); api("GET", "/encryption").then(d => { box.textContent = `${d.algorithm}, key derived with ${d.kdf.name} (N=${d.kdf.n}, r=${d.kdf.r}, p=${d.kdf.p}). SECRET comes from the environment and is never displayed. Files present: ` + Object.entries(d.files).filter(([, v]) => v).map(([k]) => k).join(", "); });
  return panel("Encryption, backup & recovery", box,
    h("p", { class: "hint" }, "Losing the SECRET makes the data unrecoverable. Backups are encrypted copies (JSON documents and images); restore and secret rotation are CLI procedures documented in docs/operations.md."),
    h("a", { class: "btn", href: "/api/backup", download: "" }, "Download encrypted backup"));
}

/* ------------------------------------------------------------ routing */
function renderNav() {
  const items = [["#/", "Dashboard"], ["#/archived", "Archived"]]; if (S.config.statistics_enabled) items.push(["#/stats", "Statistics"]); items.push(["#/settings", "Settings"]);
  const cur = location.hash || "#/";
  mount($("#nav"), items.map(([href, label]) => h("a", { href, ...(cur === href ? { "aria-current": "page" } : {}) }, label)));
}
function route() {
  clearInterval(tickTimer); RS.charts.clear(); document.documentElement.dataset.theme = S.config.theme; renderNav();
  const r = location.hash || "#/";
  ({ "#/": dashboard, "#/archived": archived, "#/stats": stats, "#/settings": settings }[r] || dashboard)();
}
window.addEventListener("hashchange", route);
load().then(route).catch(e => { $("#main").textContent = "Failed to load: " + e.message; });
