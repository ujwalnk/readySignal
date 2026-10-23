"use strict";
/* Test-only harness (jsdom). Runs the real static/*.js against real server-rendered HTML and real API payloads.
   Usage: node harness.cjs <scenarios.json>   -> prints a JSON array of results. Not used at runtime. */
const { JSDOM } = require("jsdom");
const fs = require("fs"), path = require("path");
const STATIC = path.resolve(__dirname, "../../static");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const THEMES = {
  // Mirrors the real values in static/theme.css (tests/test_frontend.py asserts that they stay in sync).
  light: { "--c1": "#0b7285", "--c2": "#c2410c", "--c3": "#4f46e5", "--c4": "#8a6a00", "--muted": "#52667b", "--line": "#d3dde8" },
  dark: { "--c1": "#3ad0e6", "--c2": "#fb923c", "--c3": "#a5b4fc", "--c4": "#facc15", "--muted": "#8da2b8", "--line": "#1f2d3e" },
};

async function run(sc) {
  const html = sc.html.replace(/<script[^>]*><\/script>/g, "");
  const dom = new JSDOM(html, { url: "http://localhost:8080/" + (sc.hash || ""), pretendToBeVisual: true, runScripts: "outside-only" });
  const w = dom.window, requests = [], errors = [], uplot = [];
  w.addEventListener("error", e => errors.push(String(e.message)));
  w.console.error = (...a) => errors.push(a.join(" "));
  const dark = !!sc.dark;
  w.matchMedia = () => ({ matches: dark, addEventListener() {} });
  w.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  w.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  w.URL.createObjectURL = () => "blob:test";
  w.URL.revokeObjectURL = () => {};
  if (sc.width) Object.defineProperty(w.HTMLElement.prototype, "clientWidth", { configurable: true, get() { return sc.width; } });
  w.getComputedStyle = () => ({ getPropertyValue: n => (THEMES[w.document.documentElement.dataset.theme === "dark" || (w.document.documentElement.dataset.theme === "system" && dark) ? "dark" : "light"][n] || "") });
  for (const [k, v] of Object.entries(sc.localStorage || {})) w.localStorage.setItem(k, v);
  w.fetch = async (url, init = {}) => {
    const u = new URL(url, "http://localhost:8080");
    requests.push({ method: init.method || "GET", path: u.pathname + u.search, headers: init.headers || {}, body: init.body || null });
    const hit = sc.routes[u.pathname + u.search] || sc.routes[u.pathname];
    const r = hit || { status: 404, body: { detail: "no route in test" } };
    return { ok: r.status >= 200 && r.status < 300, status: r.status, headers: { get: () => "application/json" }, json: async () => r.body };
  };
  w.uPlot = function (opts, data, host) {
    uplot.push({ width: opts.width, height: opts.height, series: opts.series.slice(1).map(s => ({ label: s.label, stroke: s.stroke, fill: s.fill })), data, xlabels: opts.axes[0].values({}, data[0]), xTitle: opts.axes[0].label, yTitle: opts.axes[1].label });
    this.over = w.document.createElement("div"); this.cursor = {}; this.root = w.document.createElement("div"); host.append(this.root);
    this.destroy = () => { this.root.remove(); }; this.setSize = () => {};
    opts.plugins[0].hooks.setCursor[0]({ cursor: { idx: 0, left: 5, top: 5 }, over: this.over });
    uplot[uplot.length - 1].over = this.over;  // charts.js appends the tooltip after construction; read it at the end
  };
  w.uPlot.paths = { bars: () => () => null };
  const clip = sc.clipboard || "ok";
  Object.defineProperty(w.navigator, "clipboard", { configurable: true, value: { writeText: async t => { if (clip !== "ok") throw new Error("denied"); w.__copied = t; } } });
  const scripts = sc.page === "widget" ? ["common.js", "widget.js"] : ["common.js", "charts.js", "app.js"];
  for (const f of scripts) w.eval(fs.readFileSync(path.join(STATIC, f), "utf8"));
  await sleep(250);  // first paint + fetch + requestAnimationFrame chart construction; cold jsdom can be slow
  for (const a of sc.actions || []) {
    if (a.type === "hash") { w.location.hash = a.value; w.dispatchEvent(new w.HashChangeEvent("hashchange")); }
    else if (a.type === "click") { const b = [...w.document.querySelectorAll("button, a")].find(x => (x.textContent.trim() || x.getAttribute("aria-label") || "").startsWith(a.text)); if (!b) errors.push("no button " + a.text); else b.click(); }
    else if (a.type === "clickSel") { const b = w.document.querySelector(a.selector); if (!b) errors.push("no element " + a.selector); else b.click(); }
    else if (a.type === "message") w.dispatchEvent(new w.MessageEvent("message", { data: a.data, origin: a.origin, source: a.sameSource === false ? null : w }));
    else if (a.type === "theme") { w.document.documentElement.dataset.theme = a.value; w.RS.charts.restyle(); }
    else if (a.type === "type") { const el = w.document.querySelector(a.selector); el.value = a.value; }
    else if (a.type === "submit") w.document.querySelector(a.selector).dispatchEvent(new w.Event("submit", { cancelable: true, bubbles: true }));
    await sleep(a.wait || 120);
  }
  const root = w.document.querySelector(sc.page === "widget" ? "#w" : "#main");
  const out = { text: root.textContent, html: root.innerHTML, nav: (w.document.querySelector("#nav") || { textContent: "" }).textContent, requests, errors, uplot: uplot.map(u => ({ ...u, over: undefined, tip: u.over.textContent })), copied: w.__copied || null,
    storage: Object.fromEntries(Object.keys(w.localStorage).map(k => [k, w.localStorage.getItem(k)])),
    h3: [...root.querySelectorAll("h3")].map(x => x.textContent), cards: [...root.querySelectorAll(".card, .wrow")].map(x => x.textContent.replace(/\s+/g, " ").trim()),
    names: [...root.querySelectorAll(".card h3, .wname")].map(x => x.textContent), nameHtml: [...root.querySelectorAll(".card h3")].map(x => x.innerHTML),
    scripts: root.querySelectorAll("script").length, tables: root.querySelectorAll("table").length, imgs: [...root.querySelectorAll("img")].map(i => ({ src: i.getAttribute("src"), alt: i.getAttribute("alt") })), textareas: [...w.document.querySelectorAll("textarea")].map(t => t.value),
    theme: w.document.documentElement.dataset.theme,
    header: { brand: (() => { const b = w.document.querySelector(".brand"); return b ? { href: b.getAttribute("href"), text: b.textContent.trim() } : null; })(),
      navText: (w.document.querySelector("#nav") || { textContent: "" }).textContent.trim(),
      controls: [...w.document.querySelectorAll("#nav a, #nav button")].map(x => ({ tag: x.tagName, label: x.getAttribute("aria-label"), title: x.getAttribute("title"), href: x.getAttribute("href"), text: x.textContent.trim() })) },
    cardInfo: [...root.querySelectorAll(".card, .wrow")].map(x => ({ cls: x.className, name: (x.querySelector("h3, .wname") || {}).textContent, project: (x.querySelector(".proj, .wproj") || {}).textContent, text: x.textContent.replace(/\s+/g, " ").trim(),
      controls: [...x.querySelectorAll("button, a")].map(b => ({ label: b.getAttribute("aria-label"), title: b.getAttribute("title"), text: b.textContent.trim(), disabled: b.disabled, tag: b.tagName })) })),
    dlg: !w.document.querySelector("#dlg") ? null : { open: w.document.querySelector("#dlg").hasAttribute("open"), text: w.document.querySelector("#dlg").textContent, labels: [...w.document.querySelectorAll("#dlg label")].map(l => l.firstChild ? l.firstChild.textContent : ""),
      inputs: [...w.document.querySelectorAll("#dlg input, #dlg select")].map(i => ({ name: i.name, type: i.type, value: i.value, hidden: i.closest("label") ? i.closest("label").hidden : false })) },
    fields: Object.fromEntries([...root.querySelectorAll("input, select")].filter(i => i.name).map(i => [i.name, { value: i.value, disabled: i.disabled, type: i.type }])),
    sections: [...root.querySelectorAll("section.panel")].map(sec => ({ title: (sec.querySelector("h3") || {}).textContent, links: [...sec.querySelectorAll("a")].map(a => ({ href: a.getAttribute("href"), download: a.getAttribute("download"), text: a.textContent.trim() })), text: sec.textContent })),
    overlay: (w.document.querySelector("#over") || { textContent: "" }).textContent,
    hasRoleTimer: root.querySelectorAll("[role=timer]").length };
  w.close();
  return out;
}
(async () => {
  const scenarios = JSON.parse(fs.readFileSync(process.argv[2], "utf8")), res = [];
  for (const sc of scenarios) res.push(await run(sc));
  process.stdout.write(JSON.stringify(res));
})();
