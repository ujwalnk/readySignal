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
  g.RS = Object.assign(g.RS || {}, { h, mount, fmtRemaining, fmtDuration, iconEl });
})(window);
