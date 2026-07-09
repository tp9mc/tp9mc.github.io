"""LT Parsing — краулер конкурентных площадок.

Настоящий HTTP-обход трёх источников разного типа:
  * style-hub   — HTML + sitemap.xml (discovery через карту сайта)
  * moda-market — HTML c пагинацией rel=next (discovery обходом)
  * trend-api   — JSON API с постраничной выдачей

На выходе — снапшот распарсенных офферов + полный операционный срез метрик
(coverage, freshness, quality, reliability, performance).
"""
import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from tools.common.util import (DATA_DIR, load_json, save_json, append_history,
                               iso, utcnow)

OUT_DIR = os.path.join(DATA_DIR, "parsing")
STATE_PATH = os.path.join(OUT_DIR, "state.json")
SNAPSHOT_PATH = os.path.join(OUT_DIR, "snapshot.json")
LATEST_PATH = os.path.join(OUT_DIR, "latest.json")
HISTORY_PATH = os.path.join(OUT_DIR, "history.json")

UA = "LamodaTech-Parsing/2.0 (+https://tp9mc.github.io)"
TIMEOUT = 10
RETRIES = 2
WORKERS = 12
FRESHNESS_SLA_H = 6  # источник «свежий», если успешно обкачан за последние 6ч


class Fetcher:
    """HTTP-клиент с ретраями и телеметрией по каждому запросу."""

    def __init__(self, base_url):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = UA
        self.log = []  # {url, status, ms, attempt, error}

    def get(self, path):
        url = f"{self.base}/{path.lstrip('/')}"
        last_exc = None
        for attempt in range(1, RETRIES + 2):
            t0 = time.perf_counter()
            try:
                r = self.s.get(url, timeout=TIMEOUT)
                if "charset" not in r.headers.get("content-type", "").lower():
                    r.encoding = "utf-8"  # иначе requests молча декодирует latin-1
                ms = (time.perf_counter() - t0) * 1000
                self.log.append({"url": path, "status": r.status_code,
                                 "ms": round(ms, 2), "attempt": attempt})
                if r.status_code >= 500:
                    continue
                return r
            except requests.RequestException as e:
                ms = (time.perf_counter() - t0) * 1000
                last_exc = e
                self.log.append({"url": path, "status": 0, "ms": round(ms, 2),
                                 "attempt": attempt, "error": type(e).__name__})
        if last_exc:
            return None
        return r


# ------------------------------------------------------------------ adapters

RX = {
    "sh_title": re.compile(r'<h1 class="p-title"[^>]*>(.*?)</h1>'),
    "sh_price": re.compile(r'<span class="price"[^>]*>([\d\s]+)</span>'),
    "sh_old": re.compile(r'<s class="old-price">([\d\s]+)</s>'),
    "sh_brand": re.compile(r'<span itemprop="brand">(.*?)</span>'),
    "sh_cat": re.compile(r'<div class="category">(.*?)</div>'),
    "sh_color": re.compile(r'Цвет: <b>(.*?)</b>'),
    "sh_stock": re.compile(r'Наличие: (в наличии|нет в наличии)'),
    "sh_loc": re.compile(r'<loc>(.*?)</loc>'),
    "mm_link": re.compile(r'<a class="lnk" href="(item_[^"]+)">'),
    "mm_next": re.compile(r'<a rel="next" href="([^"]+)"'),
    "mm_title": re.compile(r'<h3>(.*?)</h3>'),
    "mm_cost": re.compile(r'Цена: ([\d\s]+) руб'),
    "mm_old": re.compile(r'Было: ([\d\s]+) руб'),
    "mm_brand": re.compile(r'Производитель: (.*?)</div>'),
    "mm_cat": re.compile(r'Раздел: (.*?)</div>'),
    "mm_stock": re.compile(r'<div class="stock">(.*?)</div>'),
}


def _num(s):
    return int(re.sub(r"\D", "", s)) if s else None


def parse_style_hub(f: Fetcher, counters):
    r = f.get("shops/style-hub/sitemap.xml")
    if r is None or r.status_code != 200 or "<urlset" not in r.text:
        idx = f.get("shops/style-hub/index.html")
        if idx is not None and "Технические работы" in getattr(idx, "text", ""):
            counters["outage"] = True
        return []
    urls = [u.split("/shops/style-hub/")[-1] for u in RX["sh_loc"].findall(r.text)]
    counters["discovered"] = len(urls)

    def one(u):
        rr = f.get(f"shops/style-hub/{u}")
        if rr is None or rr.status_code != 200:
            counters["http_errors"] += 1
            return None
        m_t, m_p = RX["sh_title"].search(rr.text), RX["sh_price"].search(rr.text)
        if not m_t:
            counters["parse_errors"] += 1
            return None
        if not m_p:
            counters["field_errors"] += 1  # страница без цены — брак данных
        b, c, col = RX["sh_brand"].search(rr.text), RX["sh_cat"].search(rr.text), RX["sh_color"].search(rr.text)
        st = RX["sh_stock"].search(rr.text)
        return {"source": "style-hub", "id": u.split("/")[-1].replace(".html", ""),
                "title": m_t.group(1), "brand": b.group(1) if b else None,
                "category": c.group(1) if c else None,
                "color": col.group(1) if col else None,
                "price": _num(m_p.group(1)) if m_p else None,
                "old_price": _num(RX["sh_old"].search(rr.text).group(1)) if RX["sh_old"].search(rr.text) else None,
                "in_stock": (st.group(1) == "в наличии") if st else None,
                "url": f"shops/style-hub/{u}"}
    with ThreadPoolExecutor(WORKERS) as ex:
        return [x for x in ex.map(one, urls) if x]


def parse_moda_market(f: Fetcher, counters):
    links, page, hops = [], "shops/moda-market/index.html", 0
    while page and hops < 60:
        hops += 1
        r = f.get(page)
        if r is None or r.status_code != 200:
            counters["http_errors"] += 1
            break
        if "Технические работы" in r.text:
            counters["outage"] = True
            return []
        links += [f"shops/moda-market/{l}" for l in RX["mm_link"].findall(r.text)]
        m = RX["mm_next"].search(r.text)
        page = f"shops/moda-market/{m.group(1)}" if m else None
    links = list(dict.fromkeys(links))
    counters["discovered"] = len(links)

    def one(u):
        rr = f.get(u)
        if rr is None or rr.status_code != 200:
            counters["http_errors"] += 1
            return None
        if 'id="captcha"' in rr.text:
            counters["blocked"] += 1
            return None
        m_t = RX["mm_title"].search(rr.text)
        if not m_t:
            counters["parse_errors"] += 1
            return None
        m_p = RX["mm_cost"].search(rr.text)
        if not m_p:
            counters["field_errors"] += 1
        b, c = RX["mm_brand"].search(rr.text), RX["mm_cat"].search(rr.text)
        st = RX["mm_stock"].search(rr.text)
        return {"source": "moda-market",
                "id": u.split("item_")[-1].replace(".html", ""),
                "title": m_t.group(1), "brand": b.group(1) if b else None,
                "category": c.group(1) if c else None, "color": None,
                "price": _num(m_p.group(1)) if m_p else None,
                "old_price": _num(RX["mm_old"].search(rr.text).group(1)) if RX["mm_old"].search(rr.text) else None,
                "in_stock": (st.group(1) == "Есть на складе") if st else None,
                "url": u}
    with ThreadPoolExecutor(WORKERS) as ex:
        return [x for x in ex.map(one, links) if x]


def parse_trend_api(f: Fetcher, counters):
    r = f.get("shops/trend-api/index.json")
    if r is None or r.status_code != 200:
        counters["http_errors"] += 1
        return []
    try:
        idx = r.json()
    except ValueError:
        counters["parse_errors"] += 1
        return []
    if idx.get("error"):
        counters["outage"] = True
        return []
    out = []
    for pi in range(1, idx.get("pages", 0) + 1):
        rr = f.get(f"shops/trend-api/products_{pi}.json")
        if rr is None or rr.status_code != 200:
            counters["http_errors"] += 1
            continue
        try:
            body = rr.json()
        except ValueError:
            counters["parse_errors"] += 1  # порченый ответ API
            continue
        for it in body.get("items", []):
            if not it.get("name"):
                counters["parse_errors"] += 1
                continue
            if it.get("price") is None:
                counters["field_errors"] += 1
            out.append({"source": "trend-api", "id": it["id"], "title": it["name"],
                        "brand": it.get("brand"), "category": it.get("cat"),
                        "color": it.get("color"), "price": it.get("price"),
                        "old_price": it.get("price_old"),
                        "in_stock": it.get("available"),
                        "url": f"shops/trend-api/products_{pi}.json#{it['id']}"})
    counters["discovered"] = len(out)
    return out


ADAPTERS = {"style-hub": parse_style_hub, "moda-market": parse_moda_market,
            "trend-api": parse_trend_api}


# ------------------------------------------------------------------- metrics

def field_completeness(offers, fields=("title", "price", "brand", "category", "color", "in_stock")):
    if not offers:
        return {k: 0.0 for k in fields}
    return {k: round(sum(1 for o in offers if o.get(k) is not None) / len(offers), 4)
            for k in fields}


def run(base_url, asof=None):
    asof = asof or utcnow()
    prev_snapshot = load_json(SNAPSHOT_PATH, default={"offers": []})
    prev_by_key = {f'{o["source"]}:{o["id"]}': o for o in prev_snapshot["offers"]}
    state = load_json(STATE_PATH, default={"last_success": {}, "runs_total": 0,
                                           "runs_failed": 0})

    t_start = time.perf_counter()
    all_offers, per_source = [], {}
    for src, adapter in ADAPTERS.items():
        f = Fetcher(base_url)
        counters = {"discovered": 0, "http_errors": 0, "parse_errors": 0,
                    "field_errors": 0, "blocked": 0, "outage": False}
        t0 = time.perf_counter()
        offers = adapter(f, counters)
        dur = time.perf_counter() - t0
        ok = len(offers) > 0 and not counters["outage"]
        if ok:
            state["last_success"][src] = iso(asof)
        lat = [e["ms"] for e in f.log]
        lat_sorted = sorted(lat)
        requests_n = len(f.log)
        per_source[src] = {
            "ok": ok, "outage": counters["outage"],
            "offers": len(offers), "discovered": counters["discovered"],
            "requests": requests_n,
            "http_errors": counters["http_errors"],
            "parse_errors": counters["parse_errors"],
            "field_errors": counters["field_errors"],
            "blocked": counters["blocked"],
            "retries": sum(1 for e in f.log if e["attempt"] > 1),
            "success_rate": round(1 - (counters["http_errors"] + counters["parse_errors"]
                                       + counters["blocked"]) / max(1, requests_n), 4),
            "avg_latency_ms": round(sum(lat) / max(1, len(lat)), 2),
            "p95_latency_ms": round(lat_sorted[int(len(lat_sorted) * 0.95)], 2) if lat else 0,
            "duration_s": round(dur, 2),
            "pages_per_min": round(requests_n / dur * 60, 1) if dur > 0 else 0,
            "field_completeness": field_completeness(offers),
            "last_success": state["last_success"].get(src),
        }
        all_offers += offers

    # дельты к прошлому снапшоту
    cur_keys = {f'{o["source"]}:{o["id"]}' for o in all_offers}
    new_skus = [k for k in cur_keys if k not in prev_by_key]
    removed = [k for k in prev_by_key if k not in cur_keys]
    price_changes = []
    for o in all_offers:
        p = prev_by_key.get(f'{o["source"]}:{o["id"]}')
        if p and p.get("price") and o.get("price") and p["price"] != o["price"]:
            price_changes.append({"key": f'{o["source"]}:{o["id"]}',
                                  "old": p["price"], "new": o["price"]})

    duration = time.perf_counter() - t_start
    sources_ok = sum(1 for s in per_source.values() if s["ok"])
    state["runs_total"] += 1
    if sources_ok < len(per_source):
        state["runs_failed"] += 1

    # свежесть: возраст последней удачной обкачки каждого источника
    ages = {}
    for src, s in per_source.items():
        ls = state["last_success"].get(src)
        if ls:
            age_h = (asof - datetime.strptime(ls, "%Y-%m-%dT%H:%M:%SZ")
                     .replace(tzinfo=timezone.utc)).total_seconds() / 3600
            ages[src] = round(age_h, 2)
        else:
            ages[src] = None
    fresh_share = sum(1 for a in ages.values() if a is not None and a <= FRESHNESS_SLA_H) / len(ages)

    total_req = sum(s["requests"] for s in per_source.values())
    total_err = sum(s["http_errors"] + s["parse_errors"] + s["blocked"] for s in per_source.values())
    metrics = {
        "run_id": asof.strftime("%Y%m%d-%H%M%S"),
        "ts": iso(asof),
        "duration_s": round(duration, 2),
        "sources": per_source,
        "coverage": {
            "sources_active": sources_ok, "sources_total": len(per_source),
            "offers_parsed": len(all_offers),
            "categories_covered": len({o["category"] for o in all_offers if o["category"]}),
        },
        "quality": {
            "field_completeness": field_completeness(all_offers),
            "validation_error_rate": round(
                sum(s["field_errors"] for s in per_source.values()) / max(1, len(all_offers)), 4),
        },
        "reliability": {
            "crawl_success_rate": round(1 - total_err / max(1, total_req), 4),
            "http_errors": sum(s["http_errors"] for s in per_source.values()),
            "blocked": sum(s["blocked"] for s in per_source.values()),
            "run_uptime": round(1 - state["runs_failed"] / max(1, state["runs_total"]), 4),
        },
        "freshness": {"age_hours": ages, "fresh_share": round(fresh_share, 4),
                      "sla_hours": FRESHNESS_SLA_H},
        "performance": {
            "requests": total_req,
            "pages_per_min": round(total_req / duration * 60, 1) if duration else 0,
            "avg_latency_ms": round(sum(s["avg_latency_ms"] * s["requests"]
                                        for s in per_source.values()) / max(1, total_req), 2),
        },
        "delta": {
            "new_offers": len(new_skus), "removed_offers": len(removed),
            "price_changes": len(price_changes),
            "oos_share": round(sum(1 for o in all_offers if o.get("in_stock") is False)
                               / max(1, len(all_offers)), 4),
        },
    }

    save_json(SNAPSHOT_PATH, {"ts": iso(asof), "offers": all_offers}, compact=True)
    save_json(LATEST_PATH, metrics)
    save_json(STATE_PATH, state)
    append_history(HISTORY_PATH, _hist_entry(metrics))
    return metrics


def _hist_entry(m):
    """Компактная запись для трендов на дашборде."""
    return {
        "ts": m["ts"], "dur": m["duration_s"],
        "offers": m["coverage"]["offers_parsed"],
        "src_ok": m["coverage"]["sources_active"],
        "success": m["reliability"]["crawl_success_rate"],
        "uptime": m["reliability"]["run_uptime"],
        "fresh": m["freshness"]["fresh_share"],
        "compl_price": m["quality"]["field_completeness"]["price"],
        "compl_brand": m["quality"]["field_completeness"]["brand"],
        "new": m["delta"]["new_offers"], "removed": m["delta"]["removed_offers"],
        "price_chg": m["delta"]["price_changes"], "oos": m["delta"]["oos_share"],
        "ppm": m["performance"]["pages_per_min"],
        "lat": m["performance"]["avg_latency_ms"],
        "per_src": {k: {"offers": v["offers"], "ok": v["ok"], "sr": v["success_rate"]}
                    for k, v in m["sources"].items()},
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8010")
    ap.add_argument("--asof")
    a = ap.parse_args()
    asof = datetime.fromisoformat(a.asof).replace(tzinfo=timezone.utc) if a.asof else None
    m = run(a.base_url, asof)
    print(f'parsed={m["coverage"]["offers_parsed"]} '
          f'success={m["reliability"]["crawl_success_rate"]}')
