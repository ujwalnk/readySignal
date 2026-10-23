"use strict";
/* Graph rendering on top of the vendored uPlot build. Colours come from CSS variables, so Light/Dark/System work. */
(function (g) {
  const { h } = g.RS, live = [];
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  const short = (s, n = 14) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const INT_INCRS = [1, 2, 3, 5, 7, 10, 14, 20, 30, 60, 100, 200, 500, 1000];

  function build(host, spec) {
    const n = spec.labels.length, xs = spec.labels.map((_, i) => i), colors = spec.series.map((_, i) => css(`--c${(i % 4) + 1}`));
    const muted = css("--muted"), grid = css("--line");
    const bars = spec.mode !== "line";
    let plotted = spec.series.map(s => s.data), order = spec.series.map((_, i) => i);
    if (spec.mode === "stack") {  // cumulative values, painted tallest-first so shorter segments overlay it
      const acc = new Array(n).fill(0);
      plotted = spec.series.map(s => s.data.map((v, i) => (acc[i] += v)));
      order = order.slice().reverse();
    }
    const one = spec.series.length === 1 && bars;
    const series = [{}].concat(order.map(i => bars ? {
      label: spec.series[i].name, fill: colors[i], stroke: colors[i], width: 1,
      paths: g.uPlot.paths.bars({ size: [one ? 0.6 : 0.7, 90], gap: 2 }), points: { show: false },
    } : {
      label: spec.series[i].name, stroke: colors[i], width: 2, points: { show: n <= 60, size: 6, fill: colors[i], stroke: colors[i] },
    }));
    const tip = h("div", { class: "tip", role: "presentation", hidden: true });
    const tipPlugin = { hooks: { setCursor: [u => {
      const i = u.cursor.idx;
      if (i == null || u.cursor.left < 0) { tip.hidden = true; return; }
      tip.replaceChildren(h("strong", {}, spec.labels[i]), ...spec.series.map(s => h("div", {}, `${s.name}: ${spec.tipFmt ? spec.tipFmt(s.data[i], i, s) : s.data[i]}`)));
      tip.hidden = false;
      const w = u.over.clientWidth;
      tip.style.left = Math.max(0, Math.min(u.cursor.left + 14, w - tip.offsetWidth)) + "px";
      tip.style.top = Math.max(0, u.cursor.top - tip.offsetHeight - 8) + "px";
    }] } };
    const size = () => ({ width: Math.max(260, host.clientWidth), height: host.clientWidth < 480 ? 230 : 280 });
    const opts = {
      ...size(), series, legend: { show: false }, plugins: [tipPlugin], padding: [8, 12, 0, 0],
      cursor: { x: true, y: false, drag: { x: false, y: false }, points: { show: false } },
      scales: {
        x: { time: false, range: () => (bars ? [-0.6, n - 0.4] : n === 1 ? [-1, 1] : [-0.25, n - 0.75]) },
        y: { range: (u, lo, hi) => [0, Math.max(spec.integer ? 1 : 0.5, hi * 1.12)] },
      },
      axes: [
        { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 }, label: spec.xTitle, labelSize: 20, size: 44, space: 56, incrs: INT_INCRS,
          values: (u, vals) => vals.map(v => (Number.isInteger(v) && spec.labels[v] != null ? short(spec.labels[v]) : "")) },
        { stroke: muted, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1 }, label: spec.yTitle, labelSize: 20, size: 58,
          ...(spec.integer ? { incrs: INT_INCRS } : {}), values: (u, vals) => vals.map(v => (spec.fmtY ? spec.fmtY(v) : v)) },
      ],
    };
    const u = new g.uPlot(opts, [xs, ...order.map(i => plotted[i])], host);
    u.over.append(tip);
    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => u.setSize(size())) : null;
    ro?.observe(host);
    return { destroy() { ro?.disconnect(); u.destroy(); }, u };
  }

  function chart(host, spec) {
    host.replaceChildren();
    const entry = { host, spec, inst: build(host, spec) };
    live.push(entry);
    return entry;
  }
  function clear() { while (live.length) live.pop().inst.destroy(); }
  /* Theme switched (manual or OS): rebuild so axis/series colours follow the new CSS variables. */
  function restyle() { for (const e of live) { e.inst.destroy(); host_reset(e); } }
  function host_reset(e) { e.host.replaceChildren(); e.inst = build(e.host, e.spec); }
  g.RS.charts = { chart, clear, restyle, _live: live };
})(window);
