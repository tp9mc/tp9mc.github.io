"""LT Matching — движок сопоставления офферов конкурентов с собственным каталогом.

Пайплайн: нормализация → блокинг (категория/бренд) → скоринг пар →
пороговая маршрутизация (авто-мэтч / очередь ручной валидации / отказ) →
симуляция работы валидаторов → ценовой индекс.

Качество (precision/recall/F1) считается честно против ground truth
симулятора рынка — как на голдсете в проде.
"""
import argparse
import math
import os
import re
import time
from datetime import datetime, timezone

from tools.common.util import (DATA_DIR, load_json, save_json, append_history,
                               iso, utcnow, norm_text, tokens, trigrams)

OUT_DIR = os.path.join(DATA_DIR, "matching")
LATEST_PATH = os.path.join(OUT_DIR, "latest.json")
HISTORY_PATH = os.path.join(OUT_DIR, "history.json")
STATE_PATH = os.path.join(OUT_DIR, "state.json")      # очередь ревью + подтверждённые мэтчи
PRICE_INDEX_PATH = os.path.join(OUT_DIR, "price_index.json")

AUTO_T = 0.74      # score ≥ — авто-мэтч без человека
MARGIN = 0.04      # отрыв от второго кандидата, иначе — в ревью (неоднозначность)
REVIEW_T = 0.50    # score ≥ — кандидат в очередь валидации
REVIEW_CAPACITY = 40   # сколько карточек валидаторы разбирают за один прогон
REVIEWER_ACCURACY = 0.985
CONFIRM_TTL_DAYS = 7   # подтверждение устаревает — карточка уходит на пере-валидацию

W = {"tokens": 0.34, "trigrams": 0.12, "brand": 0.16, "price": 0.12,
     "color": 0.10, "numbers": 0.16}

# маркетинговый мусор в названиях конкурентов — не несёт сигнала
STOPWORDS = {"оригинал", "новинка", "sale", "хит", "цвет", "2026"}
_NUM_RX = re.compile(r"\d+")


def _norm_brand(b):
    return norm_text(b or "").replace("'", "").replace(" ", "")


def _prep_tokens(text):
    return {t for t in tokens(text) if t not in STOPWORDS}


def _price_sim(p1, p2):
    if not p1 or not p2:
        return 0.4  # цена неизвестна — нейтрально-низкое доверие
    return max(0.0, 1.0 - abs(math.log(p1 / p2)) / 0.45)


def build_idf(catalog):
    df = {}
    for own in catalog:
        for t in own["_tok"]:
            df[t] = df.get(t, 0) + 1
    n = len(catalog)
    return {t: math.log(n / c) for t, c in df.items()}, math.log(n)


def score_pair(offer, own, idf, idf_max):
    """Взвешенный скор пары: IDF-containment токенов + триграммы + бренд +
    цена + цвет + совпадение числовых артикулов."""
    def widf(ts):
        return sum(idf.get(t, idf_max) for t in ts)
    inter = offer["_tok"] & own["_tok"]
    tok = widf(inter) / max(1e-9, min(widf(offer["_tok"]), widf(own["_tok"])))
    a, b = offer["_tri"], own["_tri"]
    tri = 2 * len(a & b) / max(1, len(a) + len(b))
    if offer["_brand"]:
        brand = 1.0 if offer["_brand"] == own["_brand"] else 0.0
    else:
        brand = 0.5  # бренд не распарсен — не наказываем, но и не premium
    price = _price_sim(offer.get("price"), own["price"])
    if offer.get("color"):
        color = 1.0 if norm_text(offer["color"]) == norm_text(own["color"]) else 0.0
    else:
        color = 0.5
    if offer["_num"] and own["_num"]:
        num = 1.0 if offer["_num"] & own["_num"] else 0.0
    else:
        num = 0.5
    return (W["tokens"] * tok + W["trigrams"] * tri + W["brand"] * brand
            + W["price"] * price + W["color"] * color + W["numbers"] * num)


def run(asof=None):
    asof = asof or utcnow()
    t0 = time.perf_counter()
    snapshot = load_json(os.path.join(DATA_DIR, "parsing", "snapshot.json"),
                         default={"offers": []})
    catalog = load_json(os.path.join(DATA_DIR, "own_catalog.json"))["skus"]
    gt = load_json(os.path.join(DATA_DIR, "ground_truth.json"), default={})
    state = load_json(STATE_PATH, default={"queue": [], "confirmed": {},
                                           "reviewed_total": 0})

    offers = snapshot["offers"]
    for o in offers:
        o["_key"] = f'{o["source"]}:{o["id"]}'
        o["_tok"], o["_tri"] = _prep_tokens(o["title"]), trigrams(o["title"])
        o["_brand"] = _norm_brand(o.get("brand"))
        o["_num"] = set(_NUM_RX.findall(o["title"]))
    own_by_sku = {}
    block = {}  # (category) -> [own]; бренд фильтруем внутри
    for own in catalog:
        own["_tok"] = _prep_tokens(f'{own["title"]} {own["material"]}')
        own["_tri"] = trigrams(own["title"])
        own["_brand"] = _norm_brand(own["brand"])
        own["_num"] = set(_NUM_RX.findall(own["title"]))
        own_by_sku[own["sku"]] = own
        block.setdefault(norm_text(own["subtype"]), []).append(own)
    idf, idf_max = build_idf(catalog)

    pairs_scored = candidates = 0
    decisions = {}   # key -> {sku, score, decision}
    hist_buckets = [0] * 10  # score 0.5..1.0 шаг 0.05
    for o in offers:
        cand = block.get(norm_text(o.get("category") or ""), None)
        if cand is None:
            cand = catalog  # категория не распарсилась — полный проход
        if o["_brand"]:
            branded = [c for c in cand if c["_brand"] == o["_brand"]]
            cand = branded or cand
        candidates += len(cand)
        best_sku, best_score, second = None, 0.0, 0.0
        for own in cand:
            s = score_pair(o, own, idf, idf_max)
            pairs_scored += 1
            if s > best_score:
                best_sku, best_score, second = own["sku"], s, best_score
            elif s > second:
                second = s
        if best_score >= AUTO_T and best_score - second >= MARGIN:
            dec = "auto"
        elif best_score >= REVIEW_T:
            dec = "review"  # либо низкая уверенность, либо неоднозначный топ-2
        else:
            dec = "no_match"
        decisions[o["_key"]] = {"sku": best_sku, "score": round(best_score, 4),
                                "decision": dec}
        if best_score >= 0.5:
            hist_buckets[min(9, int((best_score - 0.5) / 0.05))] += 1

    # --- очередь валидации: добавляем новых кандидатов, «разбираем» capacity штук
    live_keys = set(decisions)
    # политика пере-валидации: старые подтверждения истекают и идут по новому кругу
    ttl_cut = asof.timestamp() - CONFIRM_TTL_DAYS * 86400
    state["confirmed"] = {
        k: v for k, v in state["confirmed"].items()
        if datetime.strptime(v["ts"], "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc).timestamp() >= ttl_cut}
    state["queue"] = [q for q in state["queue"] if q["key"] in live_keys]
    queued_keys = {q["key"] for q in state["queue"]}
    added_to_queue = 0
    for key, d in decisions.items():
        if d["decision"] == "review" and key not in queued_keys \
                and key not in state["confirmed"]:
            state["queue"].append({"key": key, "sku": d["sku"],
                                   "score": d["score"], "queued_at": iso(asof)})
            added_to_queue += 1
    state["queue"].sort(key=lambda q: -q["score"])  # валидируем сперва уверенные
    processed, correct_reviews = 0, 0
    import random
    rev_rng = random.Random(asof.strftime("%Y%m%d%H"))
    remaining = []
    for q in state["queue"]:
        if processed >= REVIEW_CAPACITY:
            remaining.append(q)
            continue
        processed += 1
        truth = gt.get(q["key"])
        # валидатор почти всегда решает правильно
        if rev_rng.random() < REVIEWER_ACCURACY:
            verdict_sku = truth
        else:
            verdict_sku = q["sku"] if truth != q["sku"] else None
        if verdict_sku:
            state["confirmed"][q["key"]] = {"sku": verdict_sku, "method": "review",
                                            "score": q["score"], "ts": iso(asof)}
        if verdict_sku == truth:
            correct_reviews += 1
    state["queue"] = remaining
    state["reviewed_total"] += processed

    # --- итоговый набор мэтчей: авто этого запуска + подтверждённые ранее
    state["confirmed"] = {k: v for k, v in state["confirmed"].items() if k in live_keys}
    final = {}
    for key, d in decisions.items():
        if d["decision"] == "auto":
            final[key] = {"sku": d["sku"], "method": "auto", "score": d["score"]}
    for key, v in state["confirmed"].items():
        if key not in final:
            final[key] = {"sku": v["sku"], "method": "review", "score": v["score"]}

    # --- качество против ground truth
    gt_known = {k: v for k, v in gt.items() if k in decisions}
    matchable = {k for k, v in gt_known.items() if v}
    auto_keys = {k for k, d in decisions.items() if d["decision"] == "auto"}
    auto_correct = sum(1 for k in auto_keys if gt_known.get(k) == decisions[k]["sku"])
    precision = auto_correct / max(1, len(auto_keys))
    matched_correct = sum(1 for k, v in final.items() if gt_known.get(k) == v["sku"])
    recall = sum(1 for k in matchable if k in final and final[k]["sku"] == gt_known[k]) \
        / max(1, len(matchable))
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    false_match_rate = sum(1 for k, v in final.items() if gt_known.get(k) != v["sku"]) \
        / max(1, len(final))

    # --- дубликаты и консолидация: сколько собственных SKU видят >1 оффера
    sku_offers = {}
    for k, v in final.items():
        sku_offers.setdefault(v["sku"], []).append(k)
    multi_offer_skus = sum(1 for v in sku_offers.values() if len(v) > 1)

    # --- ценовой индекс
    offer_price = {o["_key"]: o.get("price") for o in offers}
    index_rows = []
    cheaper = comparable = 0
    gaps = []
    for sku, keys in sku_offers.items():
        own = own_by_sku.get(sku)
        prices = [offer_price[k] for k in keys if offer_price.get(k)]
        if not own or not prices:
            continue
        min_comp = min(prices)
        comparable += 1
        gap = (own["price"] - min_comp) / min_comp
        gaps.append(gap)
        if own["price"] <= min_comp:
            cheaper += 1
        index_rows.append({"sku": sku, "title": own["title"], "own": own["price"],
                           "min_comp": min_comp, "offers": len(prices),
                           "gap_pct": round(gap * 100, 1)})
    index_rows.sort(key=lambda r: -abs(r["gap_pct"]))
    coverage = comparable / len(catalog)

    duration = time.perf_counter() - t0
    metrics = {
        "run_id": asof.strftime("%Y%m%d-%H%M%S"), "ts": iso(asof),
        "duration_s": round(duration, 2),
        "input": {"offers_in": len(offers), "catalog_size": len(catalog),
                  "candidates": candidates, "pairs_scored": pairs_scored},
        "funnel": {
            "auto": len(auto_keys),
            "review_new": added_to_queue,
            "no_match": sum(1 for d in decisions.values() if d["decision"] == "no_match"),
            "auto_match_rate": round(len(auto_keys) / max(1, len(offers)), 4),
            "total_match_rate": round(len(final) / max(1, len(offers)), 4),
        },
        "quality": {
            "precision_auto": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "false_match_rate": round(false_match_rate, 4),
            "review_accuracy": round(correct_reviews / max(1, processed), 4),
            "golden_set_size": len(gt_known),
        },
        "review": {
            "queue_size": len(state["queue"]),
            "added": added_to_queue, "processed": processed,
            "capacity_per_run": REVIEW_CAPACITY,
            "backlog_eta_runs": math.ceil(len(state["queue"]) / REVIEW_CAPACITY)
            if state["queue"] else 0,
            "reviewed_total": state["reviewed_total"],
        },
        "confidence_hist": {"from": 0.5, "step": 0.05, "buckets": hist_buckets},
        "consolidation": {"matched_offers": len(final),
                          "own_skus_with_offers": len(sku_offers),
                          "multi_offer_skus": multi_offer_skus},
        "price_index": {
            "coverage": round(coverage, 4),
            "competitiveness": round(cheaper / max(1, comparable), 4),
            "avg_gap_pct": round(sum(gaps) / max(1, len(gaps)) * 100, 2),
            "comparable_skus": comparable,
        },
        "performance": {"pairs_per_sec": round(pairs_scored / max(1e-9, duration)),
                        "thresholds": {"auto": AUTO_T, "review": REVIEW_T}},
    }

    save_json(LATEST_PATH, metrics)
    save_json(STATE_PATH, state, compact=True)
    save_json(PRICE_INDEX_PATH, {"ts": iso(asof), "rows": index_rows[:80],
                                 "coverage": metrics["price_index"]["coverage"],
                                 "competitiveness": metrics["price_index"]["competitiveness"]},
              compact=True)
    append_history(HISTORY_PATH, _hist_entry(metrics))
    return metrics


def _hist_entry(m):
    return {
        "ts": m["ts"], "dur": m["duration_s"],
        "offers": m["input"]["offers_in"],
        "auto_rate": m["funnel"]["auto_match_rate"],
        "match_rate": m["funnel"]["total_match_rate"],
        "precision": m["quality"]["precision_auto"],
        "recall": m["quality"]["recall"], "f1": m["quality"]["f1"],
        "queue": m["review"]["queue_size"], "reviewed": m["review"]["processed"],
        "pi_cov": m["price_index"]["coverage"],
        "pi_comp": m["price_index"]["competitiveness"],
        "gap": m["price_index"]["avg_gap_pct"],
        "pps": m["performance"]["pairs_per_sec"],
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof")
    a = ap.parse_args()
    asof = datetime.fromisoformat(a.asof).replace(tzinfo=timezone.utc) if a.asof else None
    m = run(asof)
    print(f'auto={m["funnel"]["auto"]} precision={m["quality"]["precision_auto"]} '
          f'recall={m["quality"]["recall"]} queue={m["review"]["queue_size"]}')
