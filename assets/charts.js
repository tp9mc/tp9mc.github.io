/* Мини-библиотека графиков (SVG, без зависимостей).
 * Правила: одна ось Y, тонкие марки, hairline-сетка, легенда при ≥2 сериях,
 * direct-подписи на концах линий, crosshair-тултип, табличный вид у каждого графика. */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  const fmt = {
    pct: v => (v * 100).toFixed(v * 100 >= 99.95 || v === 0 ? 0 : 1) + "%",
    pct0: v => Math.round(v * 100) + "%",
    num: v => v >= 1000 ? (v / 1000).toFixed(1).replace(".0", "") + "k" : String(Math.round(v * 10) / 10),
    int: v => String(Math.round(v)),
    sec: v => v < 60 ? v.toFixed(1) + " с" : (v / 60).toFixed(1) + " мин",
    rub: v => v.toLocaleString("ru-RU") + " ₽",
    gap: v => (v > 0 ? "+" : "") + v.toFixed(1) + "%",
    dt: ts => new Date(ts).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
    d: ts => new Date(ts).toLocaleDateString("ru-RU", { day: "2-digit", month: "2-digit" }),
  };

  function el(tag, attrs, parent) {
    const n = document.createElementNS(NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(n);
    return n;
  }
  function div(cls, parent, html) {
    const n = document.createElement("div");
    if (cls) n.className = cls;
    if (html !== undefined) n.innerHTML = html;
    if (parent) parent.appendChild(n);
    return n;
  }
  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function niceDomain(min, max, zero) {
    if (zero) min = 0;
    if (min === max) { min -= 1; max += 1; }
    const pad = (max - min) * 0.08;
    return [zero ? 0 : min - pad, max + pad];
  }

  function legend(root, series) {
    if (series.length < 2) return;
    const lg = div("legend", root);
    for (const s of series) {
      const li = div("li", lg);
      div("swatch", li).style.background = `var(${s.color})`;
      li.appendChild(document.createTextNode(s.name));
    }
  }

  /* ------------------------------------------------------------- line chart */
  function lineChart(root, cfg) {
    const H = cfg.height || 210, PL = 56, PR = cfg.direct === false ? 14 : 92, PT = 10, PB = 22;
    const series = cfg.series.filter(s => s.points.length);
    if (!series.length) { div("hint", root, "нет данных"); return; }
    legend(root, series);
    const wrapEl = div("chart", root);
    const W = Math.max(320, wrapEl.clientWidth || root.clientWidth || 640);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` }, wrapEl);

    const allTs = series[0].points.map(p => p[0]);
    const x0 = Math.min(...series.map(s => s.points[0][0]));
    const x1 = Math.max(...series.map(s => s.points[s.points.length - 1][0]));
    let vmin = Infinity, vmax = -Infinity;
    for (const s of series) for (const p of s.points) {
      if (p[1] == null) continue;
      vmin = Math.min(vmin, p[1]); vmax = Math.max(vmax, p[1]);
    }
    if (cfg.refline) { vmin = Math.min(vmin, cfg.refline.y); vmax = Math.max(vmax, cfg.refline.y); }
    let [y0, y1] = cfg.domain || niceDomain(vmin, vmax, cfg.zero);
    if ((cfg.yFmt === fmt.pct || cfg.yFmt === fmt.pct0) && vmax <= 1) {
      y1 = Math.min(y1, 1.004); y0 = Math.max(y0, 0);  // доли не рисуем выше 100%
    }
    const X = t => PL + (t - x0) / Math.max(1, x1 - x0) * (W - PL - PR);
    const Y = v => PT + (1 - (v - y0) / (y1 - y0)) * (H - PT - PB);
    const yf = cfg.yFmt || fmt.num;

    for (let i = 0; i <= 3; i++) {
      const v = y0 + (y1 - y0) * i / 3, y = Y(v);
      el("line", { x1: PL, x2: W - PR, y1: y, y2: y, class: i === 0 ? "baseline" : "gridline" }, svg);
      el("text", { x: PL - 6, y: y + 4, "text-anchor": "end", class: "tabular" }, svg)
        .textContent = yf(v);
    }
    const days = Math.max(1, Math.round((x1 - x0) / 864e5));
    const tickN = Math.min(6, days);
    for (let i = 0; i <= tickN; i++) {
      const t = x0 + (x1 - x0) * i / tickN;
      el("text", { x: X(t), y: H - 6, "text-anchor": "middle", class: "tabular" }, svg)
        .textContent = fmt.d(t);
    }
    if (cfg.refline) {
      const y = Y(cfg.refline.y);
      el("line", { x1: PL, x2: W - PR, y1: y, y2: y, class: "refline" }, svg);
      el("text", { x: W - PR - 4, y: y - 4, "text-anchor": "end" }, svg).textContent = cfg.refline.label;
    }

    for (const s of series) {
      let d = "";
      for (const p of s.points) {
        if (p[1] == null) continue;
        d += (d ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1);
      }
      el("path", { d, fill: "none", stroke: `var(${s.color})`, "stroke-width": 2,
                   "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    }

    // direct-подписи концов линий (цвет несёт точка, текст — чернильный)
    if (cfg.direct !== false && series.length <= 4) {
      const ends = series.map(s => {
        const last = [...s.points].reverse().find(p => p[1] != null);
        return { s, y: Y(last[1]), v: last[1] };
      }).sort((a, b) => a.y - b.y);
      for (let i = 1; i < ends.length; i++)
        if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
      for (const e of ends) {
        el("circle", { cx: W - PR + 6, cy: e.y, r: 3.5, fill: `var(${e.s.color})` }, svg);
        const t = el("text", { x: W - PR + 13, y: e.y + 4, class: "direct tabular" }, svg);
        t.textContent = yf(e.v);
        t.style.fill = css("--ink-1") || "";
      }
    }

    // crosshair + tooltip
    const tip = div("tip", wrapEl);
    const cross = el("line", { y1: PT, y2: H - PB, class: "gridline", visibility: "hidden" }, svg);
    const dots = series.map(s => el("circle", { r: 3.5, fill: `var(${s.color})`,
      stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" }, svg));
    const hit = el("rect", { x: PL, y: 0, width: W - PL - PR, height: H, fill: "transparent" }, svg);
    hit.addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect();
      const mx = (ev.clientX - r.left) * (W / r.width);
      const t = x0 + (mx - PL) / (W - PL - PR) * (x1 - x0);
      let idx = 0, best = Infinity;
      allTs.forEach((ts, i) => { const d = Math.abs(ts - t); if (d < best) { best = d; idx = i; } });
      const ts = allTs[idx], px = X(ts);
      cross.setAttribute("x1", px); cross.setAttribute("x2", px);
      cross.setAttribute("visibility", "visible");
      let html = `<b>${fmt.dt(ts)}</b>`;
      series.forEach((s, si) => {
        const p = s.points.find(pp => pp[0] === ts);
        if (p && p[1] != null) {
          dots[si].setAttribute("cx", px); dots[si].setAttribute("cy", Y(p[1]));
          dots[si].setAttribute("visibility", "visible");
          html += `<div class="row"><span class="swatch" style="background:var(${s.color})"></span>${s.name}: <b>${yf(p[1])}</b></div>`;
        } else dots[si].setAttribute("visibility", "hidden");
      });
      tip.innerHTML = html; tip.style.display = "block";
      const tw = tip.offsetWidth;
      tip.style.left = Math.min(wrapEl.clientWidth - tw - 4, Math.max(0, px / W * wrapEl.clientWidth + 10)) + "px";
      tip.style.top = "8px";
    });
    hit.addEventListener("mouseleave", () => {
      tip.style.display = "none"; cross.setAttribute("visibility", "hidden");
      dots.forEach(d => d.setAttribute("visibility", "hidden"));
    });

    root._tableData = {
      headers: ["Время", ...series.map(s => s.name)],
      rows: allTs.map((ts, i) => [fmt.dt(ts),
        ...series.map(s => { const p = s.points.find(pp => pp[0] === ts); return p && p[1] != null ? yf(p[1]) : "—"; })]),
    };
  }

  /* -------------------------------------------------------------- bar chart */
  function barChart(root, cfg) {
    const H = cfg.height || 200, PL = 40, PR = 8, PT = 10, PB = 26;
    const n = cfg.values.length;
    if (!n) { div("hint", root, "нет данных"); return; }
    const wrapEl = div("chart", root);
    const W = Math.max(300, wrapEl.clientWidth || root.clientWidth || 640);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` }, wrapEl);
    const vmax = Math.max(...cfg.values, 1e-9) * 1.08;
    const Y = v => PT + (1 - v / vmax) * (H - PT - PB);
    const yf = cfg.yFmt || fmt.num;
    for (let i = 0; i <= 3; i++) {
      const v = vmax * i / 3, y = Y(v);
      el("line", { x1: PL, x2: W - PR, y1: y, y2: y, class: i === 0 ? "baseline" : "gridline" }, svg);
      el("text", { x: PL - 6, y: y + 4, "text-anchor": "end", class: "tabular" }, svg).textContent = yf(v);
    }
    const slot = (W - PL - PR) / n;
    const bw = Math.min(34, Math.max(6, slot - 2));
    const tip = div("tip", wrapEl);
    const every = Math.ceil(n / Math.floor((W - PL - PR) / 46));
    cfg.values.forEach((v, i) => {
      const x = PL + slot * i + (slot - bw) / 2;
      const y = Y(v), hgt = H - PB - y, r = Math.min(4, bw / 2, Math.max(0, hgt));
      const color = cfg.colors ? cfg.colors[i] : (cfg.color || "--series-1");
      if (hgt > 0.2)
        el("path", { d: `M${x},${H - PB} V${y + r} Q${x},${y} ${x + r},${y} H${x + bw - r} Q${x + bw},${y} ${x + bw},${y + r} V${H - PB} Z`,
                     fill: `var(${color})` }, svg);
      if (i % every === 0)
        el("text", { x: x + bw / 2, y: H - 8, "text-anchor": "middle" }, svg).textContent = cfg.labels[i];
      const hit = el("rect", { x: PL + slot * i, y: PT, width: slot, height: H - PT - PB, fill: "transparent" }, svg);
      hit.addEventListener("mouseenter", () => {
        tip.innerHTML = `<b>${cfg.labels[i]}</b><div class="row">${cfg.name || ""} <b>${yf(v)}</b></div>`;
        tip.style.display = "block";
        tip.style.left = Math.min(wrapEl.clientWidth - tip.offsetWidth - 4,
          (x + bw / 2) / W * wrapEl.clientWidth) + "px";
        tip.style.top = "4px";
      });
      hit.addEventListener("mouseleave", () => { tip.style.display = "none"; });
    });
    root._tableData = {
      headers: [cfg.xName || "Значение", cfg.name || "Метрика"],
      rows: cfg.labels.map((l, i) => [l, yf(cfg.values[i])]),
    };
  }

  /* -------------------------------------------------------------- sparkline */
  function sparkline(values, color, w = 96, h = 26) {
    const vs = values.filter(v => v != null);
    if (vs.length < 2) return "";
    const mn = Math.min(...vs), mx = Math.max(...vs);
    const X = i => 2 + i / (values.length - 1) * (w - 4);
    const Y = v => 2 + (1 - (v - mn) / Math.max(1e-9, mx - mn)) * (h - 4);
    let d = "";
    values.forEach((v, i) => { if (v != null) d += (d ? "L" : "M") + X(i).toFixed(1) + "," + Y(v).toFixed(1); });
    return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
      <path d="${d}" fill="none" stroke="var(${color})" stroke-width="1.5"/></svg>`;
  }

  /* ------------------------------------------------------------ table toggle */
  function tableToggle(card, chartRoot) {
    const btn = document.createElement("button");
    btn.className = "ghost"; btn.textContent = "таблица"; btn.title = "Показать данные таблицей";
    let tbl = null;
    btn.addEventListener("click", () => {
      if (tbl) { tbl.remove(); tbl = null; btn.textContent = "таблица"; return; }
      const td = chartRoot._tableData;
      if (!td) return;
      tbl = div("table-view", card);
      const t = document.createElement("table"); t.className = "data";
      t.innerHTML = "<thead><tr>" + td.headers.map((h, i) =>
        `<th class="${i ? "num" : ""}">${h}</th>`).join("") + "</tr></thead>";
      const tb = document.createElement("tbody");
      for (const r of [...td.rows].reverse())
        tb.innerHTML += "<tr>" + r.map((c, i) => `<td class="${i ? "num" : ""}">${c}</td>`).join("") + "</tr>";
      t.appendChild(tb); tbl.appendChild(t);
      btn.textContent = "график";
    });
    return btn;
  }

  window.Viz = { fmt, lineChart, barChart, sparkline, tableToggle, div };
})();
