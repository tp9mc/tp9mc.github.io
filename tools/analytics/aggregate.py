"""Кросс-продуктовая аналитика: E2E метрики пайплайна, алерты, сводка для дашборда."""
import os

from tools.common.util import DATA_DIR, load_json, save_json, append_history, iso

SHARED_DIR = os.path.join(DATA_DIR, "shared")
ALERTS_PATH = os.path.join(SHARED_DIR, "alerts.json")
SUMMARY_PATH = os.path.join(DATA_DIR, "summary.json")
HISTORY_PATH = os.path.join(SHARED_DIR, "history.json")

E2E_SLA_S = 300  # парсинг+мэтчинг должны укладываться в 5 минут


def _alerts(asof, market_stats, pm, mm, hist):
    out = []

    def add(sev, product, code, text):
        out.append({"ts": iso(asof), "severity": sev, "product": product,
                    "code": code, "text": text})

    for src, s in pm["sources"].items():
        if s["outage"]:
            add("serious", "parsing", "source_outage",
                f"Источник {src} недоступен (maintenance) — данные не обновлены")
        elif not s["ok"]:
            add("critical", "parsing", "source_failed",
                f"Источник {src}: обкачка завершилась без данных")
        elif s["blocked"] >= 10:  # единичные капчи — фон, всплеск — сигнал
            add("warning", "parsing", "antibot",
                f"{src}: {s['blocked']} страниц с капчой — риск блокировки")
    if pm["reliability"]["crawl_success_rate"] < 0.97:
        add("warning", "parsing", "success_rate",
            f"Success rate обкачки {pm['reliability']['crawl_success_rate']:.1%} — ниже цели 97%")
    if pm["freshness"]["fresh_share"] < 1:
        stale = [k for k, v in pm["freshness"]["age_hours"].items()
                 if v is None or v > pm["freshness"]["sla_hours"]]
        add("warning", "parsing", "freshness",
            f"Свежесть вне SLA у источников: {', '.join(stale)}")

    q = mm["quality"]
    if q["precision_auto"] < 0.90:
        add("critical", "matching", "precision",
            f"Precision авто-мэтчей {q['precision_auto']:.1%} — ниже красной границы 90%")
    elif q["precision_auto"] < 0.95:
        add("warning", "matching", "precision",
            f"Precision авто-мэтчей {q['precision_auto']:.1%} — ниже цели 95%")
    if mm["review"]["queue_size"] > 150:
        add("warning", "matching", "queue",
            f"Очередь валидации {mm['review']['queue_size']} карточек "
            f"(~{mm['review']['backlog_eta_runs']} прогонов на разбор)")
    if q["recall"] < 0.80:
        add("warning", "matching", "recall",
            f"Recall {q['recall']:.1%} — теряем сопоставимые офферы")

    # деградация ценового покрытия против среднего за последние 8 прогонов
    prev = [h["pi_coverage"] for h in hist[-8:]] if hist else []
    if prev and mm["price_index"]["coverage"] < sum(prev) / len(prev) - 0.05:
        add("serious", "shared", "pi_coverage_drop",
            f"Покрытие ценового индекса упало до {mm['price_index']['coverage']:.1%}")
    return out


def aggregate(asof, market_stats, pm, mm):
    hist = load_json(HISTORY_PATH, default=[])
    alerts = _alerts(asof, market_stats, pm, mm, hist)

    # честное покрытие рынка: сколько реально существующих позиций мы увидели
    true_total = sum(s["items_true"] for s in market_stats.values())
    market_coverage = pm["coverage"]["offers_parsed"] / max(1, true_total)

    e2e = pm["duration_s"] + mm["duration_s"]
    entry = {
        "ts": iso(asof),
        "e2e_s": round(e2e, 2), "e2e_sla_ok": e2e <= E2E_SLA_S,
        "market_coverage": round(market_coverage, 4),
        "pi_coverage": mm["price_index"]["coverage"],
        "pi_competitiveness": mm["price_index"]["competitiveness"],
        "avg_gap": mm["price_index"]["avg_gap_pct"],
        "alerts": len(alerts),
        "alerts_critical": sum(1 for a in alerts if a["severity"] == "critical"),
    }
    append_history(HISTORY_PATH, entry)

    log = load_json(ALERTS_PATH, default=[])
    log += alerts
    save_json(ALERTS_PATH, log[-200:], compact=True)

    summary = {
        "ts": iso(asof),
        "pipeline": {"e2e_s": entry["e2e_s"], "sla_s": E2E_SLA_S,
                     "sla_ok": entry["e2e_sla_ok"]},
        "nsm": {
            "pi_coverage": mm["price_index"]["coverage"],
            "pi_competitiveness": mm["price_index"]["competitiveness"],
            "market_coverage": entry["market_coverage"],
        },
        "parsing": {
            "offers": pm["coverage"]["offers_parsed"],
            "sources": f'{pm["coverage"]["sources_active"]}/{pm["coverage"]["sources_total"]}',
            "success_rate": pm["reliability"]["crawl_success_rate"],
            "fresh_share": pm["freshness"]["fresh_share"],
            "uptime": pm["reliability"]["run_uptime"],
        },
        "matching": {
            "auto_match_rate": mm["funnel"]["auto_match_rate"],
            "precision": mm["quality"]["precision_auto"],
            "recall": mm["quality"]["recall"],
            "f1": mm["quality"]["f1"],
            "queue": mm["review"]["queue_size"],
        },
        "alerts_open": alerts,
    }
    save_json(SUMMARY_PATH, summary)
    return summary
