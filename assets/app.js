/* Кабина пилота: загрузка данных пайплайна и отрисовка всех вкладок. */
(function () {
  const { fmt, lineChart, barChart, sparkline, tableToggle, div } = window.Viz;
  const $ = s => document.querySelector(s);

  const SRC_COLOR = { "style-hub": "--series-1", "moda-market": "--series-2", "trend-api": "--series-3" };
  const SRC_NAME = { "style-hub": "StyleHub", "moda-market": "МодаМаркет", "trend-api": "TrendAPI" };
  const SEV_NAME = { warning: "внимание", serious: "серьёзный", critical: "критичный" };

  let D = {};          // все загруженные данные
  let rangeDays = 14;  // фильтр диапазона

  const J = p => fetch(p, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null);

  async function load() {
    const [summary, pL, pH, mL, mH, sH, alerts, pi, hq] = await Promise.all([
      J("data/summary.json"),
      J("data/parsing/latest.json"), J("data/parsing/history.json"),
      J("data/matching/latest.json"), J("data/matching/history.json"),
      J("data/shared/history.json"), J("data/shared/alerts.json"),
      J("data/matching/price_index.json"), J("data/shared/hq.json"),
    ]);
    D = { summary, pL, pH: pH || [], mL, mH: mH || [], sH: sH || [], alerts: alerts || [], pi, hq };
  }

  const inRange = h => {
    if (!h.length) return h;
    const cut = Date.parse(h[h.length - 1].ts) - rangeDays * 864e5;
    return h.filter(e => Date.parse(e.ts) >= cut);
  };
  const pts = (hist, key) => hist.map(e => [Date.parse(e.ts), typeof key === "function" ? key(e) : e[key]]);

  /* ------------------------------------------------------------ scaffolding */
  function card(parent, title, hint, cls) {
    const c = div("card" + (cls ? " " + cls : ""), parent);
    const head = div("card-head", c);
    head.innerHTML = `<h3>${title}</h3><div class="spacer"></div>`;
    if (hint) div("hint", c, hint);
    const body = div("", c);
    return { c, head, body };
  }
  function chartCard(parent, title, hint, cls, draw) {
    const { c, head, body } = card(parent, title, hint, cls);
    draw(body);
    head.appendChild(tableToggle(c, body));
    return c;
  }
  const ago = ts => {
    const m = Math.round((Date.now() - Date.parse(ts)) / 60000);
    if (m < 1) return "только что";
    if (m < 60) return `${m} мин назад`;
    if (m < 48 * 60) return `${Math.round(m / 60)} ч назад`;
    return `${Math.round(m / 1440)} дн назад`;
  };

  /* ----------------------------------------------------------------- header */
  function renderHeader() {
    const s = D.summary;
    const box = $("#status");
    box.innerHTML = "";
    if (!s) { box.innerHTML = '<span class="pill"><span class="dot crit"></span>нет данных</span>'; return; }
    const crit = s.alerts_open.filter(a => a.severity === "critical").length;
    const cls = crit ? "crit" : (s.alerts_open.length ? "warn" : "ok");
    const stale = Date.now() - Date.parse(s.ts) > 12 * 3600e3;
    box.innerHTML =
      `<span class="pill"><span class="dot ${cls}"></span>прогон ${ago(s.ts)}${stale ? " · данные устарели" : ""}</span>` +
      `<span class="pill">E2E ${fmt.sec(s.pipeline.e2e_s)} / SLA ${fmt.sec(s.pipeline.sla_s)}</span>` +
      (s.alerts_open.length ? `<span class="pill"><span class="dot ${cls}"></span>алертов: ${s.alerts_open.length}</span>`
                            : `<span class="pill"><span class="dot ok"></span>алертов нет</span>`);
    const hq = D.hq;
    if (hq) {
      const t = hq.reachable
        ? `<span class="dot ok"></span>lamoda.ru · ${hq.latency_ms} мс`
        : (hq.error === "sandbox"
          ? `<span class="dot warn"></span>lamoda.ru · проверка из CI`
          : `<span class="dot crit"></span>lamoda.ru · нет связи`);
      box.innerHTML += `<a class="pill" href="https://www.lamoda.ru/" title="Связь с головной компанией, проверяется каждый прогон">${t}</a>`;
    }
  }

  /* ------------------------------------------------------------------- KPIs */
  function kpi(parent, label, value, deltaTxt, deltaCls, sparkHtml, title) {
    const k = div("kpi", parent);
    if (title) k.title = title;
    k.innerHTML = `<div class="label">${label}</div><div class="value">${value}</div>` +
      (deltaTxt ? `<div class="delta ${deltaCls || ""}">${deltaTxt}</div>` : "") + (sparkHtml || "");
  }
  function delta(hist, key, f, goodUp = true) {
    const h = inRange(hist);
    if (h.length < 2) return null;
    const a = h[h.length - 2][key], b = h[h.length - 1][key];
    if (a == null || b == null) return null;
    const d = b - a;
    const cls = d === 0 ? "" : ((d > 0) === goodUp ? "up" : "down");
    return { txt: (d > 0 ? "▲ " : d < 0 ? "▼ " : "= ") + f(Math.abs(d)), cls };
  }
  function renderKpis() {
    const root = $("#kpis"); root.innerHTML = "";
    const s = D.summary; if (!s) return;
    const mh = inRange(D.mH), sh = inRange(D.sH), ph = inRange(D.pH);
    const sp = (h, k, c) => sparkline(h.map(e => e[k]), c);
    let d;
    d = delta(D.mH, "pi_cov", fmt.pct);
    kpi(root, "Покрытие ценового индекса", fmt.pct(s.nsm.pi_coverage), d && d.txt, d && d.cls,
        sp(mh, "pi_cov", "--series-2"), "Доля собственного каталога, у которой есть сопоставленный оффер конкурента (NSM)");
    d = delta(D.mH, "pi_comp", fmt.pct);
    kpi(root, "Ценовая конкурентоспособность", fmt.pct(s.nsm.pi_competitiveness), d && d.txt, d && d.cls,
        sp(mh, "pi_comp", "--series-2"), "Доля сопоставимых SKU, где наша цена ≤ минимальной у конкурентов");
    d = delta(D.sH, "market_coverage", fmt.pct);
    kpi(root, "Покрытие рынка парсингом", fmt.pct(s.nsm.market_coverage), d && d.txt, d && d.cls,
        sp(sh, "market_coverage", "--series-1"), "Доля реально существующих позиций конкурентов, которые мы увидели в этом прогоне");
    d = delta(D.pH, "success", fmt.pct);
    kpi(root, "Success rate обкачки", fmt.pct(s.parsing.success_rate), d && d.txt, d && d.cls,
        sp(ph, "success", "--series-1"), "Доля запросов без HTTP-ошибок, капчи и ошибок парсинга");
    d = delta(D.mH, "precision", fmt.pct);
    kpi(root, "Precision авто-мэтчей", fmt.pct(s.matching.precision), d && d.txt, d && d.cls,
        sp(mh, "precision", "--series-2"), "Точность автоматических сопоставлений против голдсета");
    d = delta(D.mH, "recall", fmt.pct);
    kpi(root, "Recall мэтчинга", fmt.pct(s.matching.recall), d && d.txt, d && d.cls,
        sp(mh, "recall", "--series-2"), "Доля сопоставимых офферов, которые нашли (авто + валидация)");
  }

  /* --------------------------------------------------------------- overview */
  function healthRows(el, rows) {
    const h = div("health", el);
    for (const [k, v, cls] of rows)
      div("row", h, `<span class="k">${k}</span><span class="v ${cls || ""}">${v}</span>`);
  }
  function renderOverview() {
    const root = $("#tab-overview"); root.innerHTML = "";
    const g = div("grid", root);
    const s = D.summary, pl = D.pL, ml = D.mL;
    if (!s) return;

    const p = card(g, "LT Parsing — здоровье продукта", "операционный срез последнего прогона");
    healthRows(p.body, [
      ["Офферов собрано", fmt.int(s.parsing.offers)],
      ["Источники активны", s.parsing.sources],
      ["Success rate", fmt.pct(s.parsing.success_rate)],
      ["Свежесть в SLA", fmt.pct(s.parsing.fresh_share)],
      ["Uptime прогонов", fmt.pct(s.parsing.uptime)],
      ["Длительность", fmt.sec(pl ? pl.duration_s : 0)],
    ]);
    const m = card(g, "LT Matching — здоровье продукта", "качество и воронка последнего прогона");
    healthRows(m.body, [
      ["Auto-match rate", fmt.pct(s.matching.auto_match_rate)],
      ["Precision (авто)", fmt.pct(s.matching.precision)],
      ["Recall", fmt.pct(s.matching.recall)],
      ["F1", fmt.pct(s.matching.f1)],
      ["Очередь валидации", fmt.int(s.matching.queue) + " карт."],
      ["Скорость", ml ? fmt.num(ml.performance.pairs_per_sec) + " пар/с" : "—"],
    ]);

    chartCard(g, "North Star: ценовой индекс", "покрытие и конкурентоспособность собственного каталога", "w12", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Покрытие индекса", color: "--series-2", points: pts(inRange(D.mH), "pi_cov") },
        { name: "Конкурентоспособность", color: "--series-4", points: pts(inRange(D.mH), "pi_comp") },
        { name: "Покрытие рынка", color: "--series-1", points: pts(inRange(D.sH), "market_coverage") },
      ]}));

    const a = card(g, "Открытые алерты последнего прогона", "", "w12");
    if (!s.alerts_open.length) a.body.innerHTML = '<span class="badge good">всё зелёное — алертов нет</span>';
    else for (const al of s.alerts_open) alertRow(a.body, al);
  }
  function alertRow(el, al) {
    const r = div("alert-row", el);
    r.innerHTML = `<time>${fmt.dt(Date.parse(al.ts))}</time>` +
      `<span class="badge ${al.severity}">${SEV_NAME[al.severity] || al.severity}</span>` +
      `<span class="prod">${al.product}</span><span>${al.text}</span>`;
  }

  /* ---------------------------------------------------------------- parsing */
  function renderParsing() {
    const root = $("#tab-parsing"); root.innerHTML = "";
    const g = div("grid", root);
    const h = inRange(D.pH), pl = D.pL;

    chartCard(g, "Собранные офферы по источникам", "объём выгрузки за прогон", "w12", el =>
      lineChart(el, { yFmt: fmt.int, zero: true, series: Object.keys(SRC_COLOR).map(src => ({
        name: SRC_NAME[src], color: SRC_COLOR[src],
        points: pts(h, e => e.per_src && e.per_src[src] ? e.per_src[src].offers : null),
      }))}));

    chartCard(g, "Надёжность обкачки", "success rate запросов и uptime прогонов", "", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Success rate", color: "--series-1", points: pts(h, "success") },
        { name: "Uptime прогонов", color: "--series-5", points: pts(h, "uptime") },
      ]}));

    chartCard(g, "Свежесть данных", `доля источников, обкачанных за последние ${pl ? pl.freshness.sla_hours : 6} ч (SLA)`, "", el =>
      lineChart(el, { yFmt: fmt.pct0, domain: [0, 1.05], series: [
        { name: "Свежесть в SLA", color: "--series-1", points: pts(h, "fresh") },
      ]}));

    const fc = card(g, "Полнота полей", "доля офферов с заполненным полем (последний прогон)");
    if (pl) {
      const names = { title: "Название", price: "Цена", brand: "Бренд", category: "Категория", color: "Цвет", in_stock: "Наличие" };
      for (const [k, v] of Object.entries(pl.quality.field_completeness)) {
        const row = div("meter-row", fc.body);
        row.innerHTML = `<span class="name">${names[k] || k}</span>` +
          `<span class="meter"><i style="width:${(v * 100).toFixed(1)}%"></i></span>` +
          `<span class="val">${fmt.pct(v)}</span>`;
      }
    }

    chartCard(g, "Динамика ассортимента", "новые и исчезнувшие офферы за прогон", "", el =>
      lineChart(el, { yFmt: fmt.int, zero: true, series: [
        { name: "Новые", color: "--series-4", points: pts(h, "new") },
        { name: "Исчезли", color: "--series-6", points: pts(h, "removed") },
      ]}));

    chartCard(g, "Сигналы рынка", "изменения цен за прогон и доля out-of-stock", "", el =>
      lineChart(el, { yFmt: fmt.int, zero: true, series: [
        { name: "Изменений цен", color: "--series-3", points: pts(h, "price_chg") },
      ]}));

    chartCard(g, "Производительность", "страниц в минуту (локальный стенд)", "", el =>
      lineChart(el, { yFmt: fmt.num, zero: true, series: [
        { name: "Стр/мин", color: "--series-1", points: pts(h, "ppm") },
      ]}));

    chartCard(g, "Доля out-of-stock", "доля офферов «нет в наличии»", "", el =>
      lineChart(el, { yFmt: fmt.pct, zero: true, series: [
        { name: "OOS", color: "--series-6", points: pts(h, "oos") },
      ]}));

    const t = card(g, "Источники — последний прогон", "", "w12");
    if (pl) {
      const rows = Object.entries(pl.sources).map(([src, v]) => `<tr>
        <td><span class="badge ${v.ok ? "good" : (v.outage ? "serious" : "critical")}">${v.ok ? "ок" : (v.outage ? "maintenance" : "сбой")}</span></td>
        <td>${SRC_NAME[src]}</td>
        <td class="num">${v.offers}</td><td class="num">${v.discovered}</td>
        <td class="num">${v.requests}</td><td class="num">${fmt.pct(v.success_rate)}</td>
        <td class="num">${v.http_errors}</td><td class="num">${v.parse_errors}</td>
        <td class="num">${v.blocked}</td><td class="num">${v.retries}</td>
        <td class="num">${v.avg_latency_ms} / ${v.p95_latency_ms}</td>
        <td class="num">${v.last_success ? ago(v.last_success) : "—"}</td></tr>`).join("");
      t.body.innerHTML = `<div class="scroll-x"><table class="data"><thead><tr>
        <th>Статус</th><th>Источник</th><th class="num">Офферы</th><th class="num">Найдено</th>
        <th class="num">Запросы</th><th class="num">Success</th><th class="num">HTTP-ошиб.</th>
        <th class="num">Парс-ошиб.</th><th class="num">Капчи</th><th class="num">Ретраи</th>
        <th class="num">Задержка avg/p95, мс</th><th class="num">Успех был</th>
        </tr></thead><tbody>${rows}</tbody></table></div>`;
    }
  }

  /* --------------------------------------------------------------- matching */
  function renderMatching() {
    const root = $("#tab-matching"); root.innerHTML = "";
    const g = div("grid", root);
    const h = inRange(D.mH), ml = D.mL;

    if (ml) {
      const f = card(g, "Воронка последнего прогона", "маршрутизация офферов по порогам уверенности", "w12");
      healthRows(f.body, [
        ["Офферов на входе", fmt.int(ml.input.offers_in)],
        ["Пар оценено", ml.input.pairs_scored.toLocaleString("ru-RU")],
        ["Авто-мэтч (score ≥ " + ml.performance.thresholds.auto + ")", fmt.int(ml.funnel.auto)],
        ["Отправлено в валидацию", fmt.int(ml.funnel.review_new)],
        ["Без мэтча", fmt.int(ml.funnel.no_match)],
        ["Итоговый match rate (с валидацией)", fmt.pct(ml.funnel.total_match_rate)],
      ]);
    }

    chartCard(g, "Качество против голдсета", "precision / recall / F1", "", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Precision (авто)", color: "--series-2", points: pts(h, "precision") },
        { name: "Recall", color: "--series-1", points: pts(h, "recall") },
        { name: "F1", color: "--series-5", points: pts(h, "f1") },
      ]}));

    chartCard(g, "Match rate", "доля офферов, сведённых с каталогом", "", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Авто", color: "--series-2", points: pts(h, "auto_rate") },
        { name: "Итого (с валидацией)", color: "--series-4", points: pts(h, "match_rate") },
      ]}));

    chartCard(g, "Очередь ручной валидации", ml ? `мощность разбора — ${ml.review.capacity_per_run} карточек за прогон` : "", "", el =>
      lineChart(el, { yFmt: fmt.int, zero: true, series: [
        { name: "В очереди", color: "--series-3", points: pts(h, "queue") },
        { name: "Разобрано за прогон", color: "--series-1", points: pts(h, "reviewed") },
      ]}));

    chartCard(g, "Распределение уверенности", "число офферов по скору лучшего кандидата (последний прогон)", "", el => {
      if (!ml) return;
      const { from, step, buckets } = ml.confidence_hist;
      barChart(el, { name: "офферов", xName: "Скор",
        labels: buckets.map((_, i) => (from + i * step).toFixed(2)),
        values: buckets, color: "--series-2", yFmt: fmt.int });
    });

    chartCard(g, "Ценовой индекс", "покрытие каталога и доля SKU с лучшей ценой", "", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Покрытие", color: "--series-2", points: pts(h, "pi_cov") },
        { name: "Конкурентоспособность", color: "--series-4", points: pts(h, "pi_comp") },
      ]}));

    chartCard(g, "Средний ценовой разрыв", "наша цена против минимума конкурентов, %", "", el =>
      lineChart(el, { yFmt: fmt.gap, refline: { y: 0, label: "паритет" }, series: [
        { name: "Разрыв", color: "--series-6", points: pts(h, "gap") },
      ]}));

    const t = card(g, "Прайс-индекс: наибольшие отклонения", "SKU с максимальным разрывом к минимальной цене конкурента", "w12");
    if (D.pi && D.pi.rows.length) {
      const rows = D.pi.rows.slice(0, 25).map(r => {
        // SKU демо-каталога нет на реальном сайте — ищем похожие: тип + бренд
        const q = r.q || r.title.split(",")[0].split(" ").filter(w => !/^\d+$/.test(w)).slice(0, -1).join(" ");
        return `<tr>
        <td class="mono">${r.sku}</td>
        <td><a href="https://www.lamoda.ru/catalogsearch/result/?q=${encodeURIComponent(q)}"
               title="Похожие товары в каталоге lamoda.ru (SKU — демо-каталог): ${q}"
               target="_blank" rel="noopener">${r.title}</a></td>
        <td class="num">${fmt.rub(r.own)}</td><td class="num">${fmt.rub(r.min_comp)}</td>
        <td class="num">${r.offers}</td>
        <td class="num" style="font-weight:600">${fmt.gap(r.gap_pct)}</td></tr>`;
      }).join("");
      t.body.innerHTML = `<div class="scroll-x"><table class="data"><thead><tr>
        <th>SKU</th><th>Товар</th><th class="num">Наша цена</th>
        <th class="num">Мин. у конкурентов</th><th class="num">Офферов</th>
        <th class="num">Разрыв</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    }
  }

  /* ----------------------------------------------------------------- shared */
  function renderShared() {
    const root = $("#tab-shared"); root.innerHTML = "";
    const g = div("grid", root);
    const h = inRange(D.sH);

    chartCard(g, "E2E длительность пайплайна", "парсинг + мэтчинг против SLA", "", el =>
      lineChart(el, { yFmt: fmt.sec, zero: true, refline: { y: 300, label: "SLA 5 мин" }, series: [
        { name: "E2E", color: "--series-1", points: pts(h, "e2e_s") },
      ]}));

    chartCard(g, "Покрытие рынка (end-to-end)", "сколько реального ассортимента конкурентов доходит до индекса", "", el =>
      lineChart(el, { yFmt: fmt.pct, series: [
        { name: "Покрытие рынка", color: "--series-1", points: pts(h, "market_coverage") },
        { name: "Покрытие индекса", color: "--series-2", points: pts(h, "pi_coverage") },
      ]}));

    chartCard(g, "Алерты за прогон", "сигналы деградации по обоим продуктам", "", el =>
      lineChart(el, { yFmt: fmt.int, zero: true, series: [
        { name: "Все алерты", color: "--series-3", points: pts(h, "alerts") },
        { name: "Критичные", color: "--series-6", points: pts(h, "alerts_critical") },
      ]}));

    chartCard(g, "Ценовой разрыв", "средняя разница нашей цены к минимуму рынка", "", el =>
      lineChart(el, { yFmt: fmt.gap, refline: { y: 0, label: "паритет" }, series: [
        { name: "Средний разрыв", color: "--series-6", points: pts(h, "avg_gap") },
      ]}));
  }

  /* -------------------------------------------------------------- incidents */
  function renderIncidents() {
    const root = $("#tab-incidents"); root.innerHTML = "";
    const g = div("grid", root);
    const list = [...D.alerts].reverse();
    const counts = {};
    for (const a of D.alerts) counts[a.severity] = (counts[a.severity] || 0) + 1;
    const c = card(g, "Журнал инцидентов", `последние ${list.length} записей · критичных: ${counts.critical || 0}, серьёзных: ${counts.serious || 0}, предупреждений: ${counts.warning || 0}`, "w12");
    if (!list.length) c.body.innerHTML = '<span class="badge good">журнал пуст</span>';
    for (const al of list.slice(0, 120)) alertRow(c.body, al);
  }

  /* ------------------------------------------------------------------ tabs */
  function initTabs() {
    document.querySelectorAll("nav.tabs button").forEach(b =>
      b.addEventListener("click", () => {
        document.querySelectorAll("nav.tabs button").forEach(x => x.classList.toggle("active", x === b));
        document.querySelectorAll(".tab-page").forEach(p =>
          p.classList.toggle("active", p.id === "tab-" + b.dataset.tab));
      }));
    document.querySelectorAll("#range button").forEach(b =>
      b.addEventListener("click", () => {
        rangeDays = +b.dataset.days;
        document.querySelectorAll("#range button").forEach(x => x.classList.toggle("active", x === b));
        renderAll();
      }));
    const saved = localStorage.getItem("theme");
    if (saved) document.documentElement.dataset.theme = saved;
    $("#theme").addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme
        || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("theme", next);
      renderAll();
    });
  }

  function renderAll() {
    renderHeader(); renderKpis(); renderOverview();
    renderParsing(); renderMatching(); renderShared(); renderIncidents();
  }

  async function main() {
    initTabs();
    await load();
    renderAll();
    setInterval(async () => { await load(); renderAll(); }, 5 * 60 * 1000);
  }
  main();
})();
