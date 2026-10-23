"use strict";
/* Shared helpers for the dashboard and the widget. No framework, no build step. */
(function (g) {
  const skip = k => k == null || k === false || k === true;

  /* Hyperscript. Children that are null/undefined/booleans are SKIPPED. Never hand them to the DOM directly:
     Element.replaceChildren(null) and document.createTextNode(null) stringify them into a visible "null". */
  function h(tag, attrs = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const kid of kids.flat(Infinity)) if (!skip(kid)) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  /* The only sanctioned way to replace an element's content: optional pieces may safely be null/false. */
  function mount(el, ...kids) { el.replaceChildren(...kids.flat(Infinity).filter(k => !skip(k))); return el; }

  const p2 = n => String(n).padStart(2, "0");
  function fmtRemaining(ms) {
    const s = Math.max(0, Math.ceil(ms / 1000)), d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
    return (d ? d + "d " : "") + `${p2(hh)}:${p2(m)}:${p2(ss)}`;
  }
  function fmtDuration(sec) {
    const d = Math.floor(sec / 86400), hh = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
    return [d && d + "d", hh && hh + "h", m && m + "m"].filter(Boolean).join(" ") || "0m";
  }
  /* Image with a graceful fallback: server-validated asset if present, otherwise the emoji (or a default glyph). */
  function iconEl(a, cls = "icon") {
    const fallback = () => h("span", { class: cls, "aria-hidden": "true" }, a.icon || "⏳");
    if (!a.icon_url) return fallback();
    const img = h("img", { class: cls, src: a.icon_url, alt: `${a.name} icon`, width: 48, height: 48, decoding: "async" });
    img.addEventListener("error", () => img.replaceWith(fallback()), { once: true });
    return img;
  }
  /* Inline SVG icons (stroke geometry adapted from Lucide, ISC licence). Built with createElementNS: no innerHTML. */
  const NS = "http://www.w3.org/2000/svg";
  const PATHS = {
    plus: ["M12 5v14", "M5 12h14"],
    gear: ["M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"],
    edit: ["M17 3a2.85 2.85 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z", "m15 5 4 4"],
    copy: ["M9 9h11a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1z", "M5 15H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1v1"],
    play: ["M6 4l14 8-14 8z"],
    extend: ["M12 7v6l3 2", "M12 21a8 8 0 1 0 0-16 8 8 0 0 0 0 16z", "M5 3 2 6", "m22 6-3-3"],
    download: ["M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4", "m7 10 5 5 5-5", "M12 15V3"],
    back: ["M19 12H5", "m12 19-7-7 7-7"],
    archive: ["M21 8v13H3V8", "M1 3h22v5H1z", "M10 12h4"],
  };
  function icon(name, cls = "ico") {
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("class", cls); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("focusable", "false");
    for (const d of PATHS[name] || []) { const p = document.createElementNS(NS, "path"); p.setAttribute("d", d); svg.append(p); }
    return svg;
  }
  /* Icon-only control with an accessible name and a tooltip. `tag` is "button" or "a". */
  function ibtn(name, label, attrs = {}, tag = "button") {
    return h(tag, { class: "ibtn", "aria-label": label, title: label, ...(tag === "button" ? { type: "button" } : {}), ...attrs }, icon(name));
  }
  g.RS = Object.assign(g.RS || {}, { h, mount, fmtRemaining, fmtDuration, iconEl, icon, ibtn });
})(window);
