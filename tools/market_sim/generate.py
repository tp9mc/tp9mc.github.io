"""Симулятор рынка: собственный каталог + три демо-магазина конкурентов.

Магазины — настоящие сайты (статические HTML/JSON), которые краулер LT Parsing
обходит по HTTP. Рынок «живёт»: цены дрейфуют, ассортимент ротируется,
страницы иногда ломаются, магазин может уйти в maintenance — всё
детерминировано от метки времени запуска, поэтому история воспроизводима.
"""
import argparse
import os
import shutil
from datetime import datetime, timezone

from tools.common.util import ROOT, DATA_DIR, SHOPS_DIR, rng_for, load_json, save_json, iso

CATALOG_PATH = os.path.join(DATA_DIR, "own_catalog.json")
GROUND_TRUTH_PATH = os.path.join(DATA_DIR, "ground_truth.json")

CATEGORIES = {
    "Обувь": ["Кроссовки", "Кеды", "Ботинки", "Туфли", "Сандалии"],
    "Одежда": ["Футболка", "Худи", "Джинсы", "Платье", "Куртка",
               "Свитер", "Рубашка", "Юбка", "Брюки", "Пальто"],
    "Аксессуары": ["Рюкзак", "Сумка", "Ремень", "Шапка", "Очки"],
}
PRICE_RANGE = {"Обувь": (2990, 18990), "Одежда": (990, 15990), "Аксессуары": (590, 9990)}
BRANDS = ["Nike", "adidas", "Puma", "Reebok", "New Balance", "Levi's", "Mango",
          "Guess", "Tommy Hilfiger", "Calvin Klein", "Lacoste", "O'STIN", "Befree",
          "Zarina", "Tom Tailor", "Vans", "Converse", "Columbia", "The North Face", "GAP"]
COLORS = ["белый", "чёрный", "серый", "синий", "красный", "зелёный", "бежевый",
          "коричневый", "розовый", "голубой", "бордовый", "хаки"]
MATERIALS = ["хлопок", "полиэстер", "кожа", "замша", "деним", "шерсть", "вискоза", "текстиль"]
MODEL_WORDS = ["Classic", "Urban", "Air", "Retro", "Sport", "Street", "Original",
               "Essential", "Premium", "Basic", "Flex", "Pro", "Lite", "Max", "City"]
GENDERS = ["мужской", "женский", "унисекс"]

OWN_CATALOG_SIZE = 900

SHOPS = {
    "style-hub": {
        "name": "StyleHub", "kind": "html-sitemap", "page_size": 24,
        "share_own": 0.58, "extra_unique": 40, "price_factor": 1.03,
        "noise": 0.38, "broken_rate": 0.012, "missing_link_rate": 0.004,
        "outage_rate": 0.01,
    },
    "moda-market": {
        "name": "ModaMarket", "kind": "html-pagination", "page_size": 20,
        "share_own": 0.46, "extra_unique": 55, "price_factor": 0.97,
        "noise": 0.72, "broken_rate": 0.03, "missing_link_rate": 0.01,
        "outage_rate": 0.035, "captcha_rate": 0.015,
    },
    "trend-api": {
        "name": "TrendAPI", "kind": "json-api", "page_size": 50,
        "share_own": 0.36, "extra_unique": 70, "price_factor": 1.00,
        "noise": 0.50, "broken_rate": 0.008, "missing_link_rate": 0.0,
        "outage_rate": 0.02,
    },
}


# ---------------------------------------------------------------- own catalog

def ensure_own_catalog():
    cat = load_json(CATALOG_PATH)
    if cat:
        return cat
    rng = rng_for("own-catalog", "v1")
    skus, seen = [], set()
    i = 0
    while len(skus) < OWN_CATALOG_SIZE:
        i += 1
        cat_name = rng.choice(list(CATEGORIES))
        subtype = rng.choice(CATEGORIES[cat_name])
        brand = rng.choice(BRANDS)
        model = rng.choice(MODEL_WORDS) + (f" {rng.randint(1, 99)}" if rng.random() < 0.5 else "")
        color = rng.choice(COLORS)
        key = (subtype, brand, model, color)
        if key in seen:
            continue
        seen.add(key)
        lo, hi = PRICE_RANGE[cat_name]
        price = round(rng.uniform(lo, hi) / 10) * 10 - 1
        skus.append({
            "sku": f"LMD-{i:06d}",
            "title": f"{subtype} {brand} {model}, {color}",
            "brand": brand, "category": cat_name, "subtype": subtype,
            "gender": rng.choice(GENDERS), "color": color,
            "material": rng.choice(MATERIALS), "price": price,
        })
    cat = {"generated_at": "2026-07-09T00:00:00Z", "skus": skus}
    save_json(CATALOG_PATH, cat)
    return cat


# ------------------------------------------------------------- title noising

def noisy_title(item, shop_rng, noise_level):
    """Название у конкурента: перестановки, потери слов, маркетинговый мусор."""
    brand = item["brand"]
    parts = [item["subtype"], brand, item["title"].split(", ")[0].split(f"{brand} ", 1)[-1]]
    title = item["title"]
    r = shop_rng.random()
    if r < noise_level:
        model = title.split(", ")[0].replace(f"{item['subtype']} ", "", 1)
        variants = [
            f"{model} — {item['subtype'].lower()} ({item['color']})",
            f"{item['subtype']} {model.upper()}",
            f"{model}, цвет {item['color']}",
            f"{item['subtype']} {brand} / {item['color']}, {item['material']}",
            f"{title} {shop_rng.choice(['оригинал', 'новинка 2026', 'sale', 'хит'])}",
            # тяжёлые случаи: потерян модельный номер — главный сигнал
            f"{item['subtype']} {brand}, {item['color']}",
            f"{item['subtype']} {brand} {item['material']}",
        ]
        title = shop_rng.choice(variants)
    if shop_rng.random() < noise_level * 0.3:
        title = title.replace("ё", "е").lower()
    return title


# ------------------------------------------------------------- market layout

def build_shop_assortment(shop_id, cfg, catalog, asof):
    """Ассортимент магазина на момент asof: подмножество нашего каталога + уникальные позиции."""
    week = asof.strftime("%G-W%V")
    day = asof.strftime("%Y-%m-%d")
    items = []
    for own in catalog["skus"]:
        base = rng_for("carry", shop_id, own["sku"]).random()          # стабильная база
        rotation = rng_for("rot", shop_id, own["sku"], week).random()  # недельная ротация
        present = base < cfg["share_own"] and rotation > 0.12
        if not present:
            continue
        r = rng_for("item", shop_id, own["sku"], day)
        drift = rng_for("price", shop_id, own["sku"], day).uniform(-0.12, 0.12)
        price = own["price"] * cfg["price_factor"] * (1 + drift)
        # внутридневной репрайсинг: часть позиций переоценивается каждый прогон
        flash = rng_for("flash", shop_id, own["sku"], asof.strftime("%Y-%m-%d %H"))
        if flash.random() < 0.10:
            price *= flash.uniform(0.94, 1.06)
        price = max(190, round(price / 10) * 10 - 1)
        old_price = round(price * r.uniform(1.15, 1.5) / 10) * 10 - 1 if r.random() < 0.3 else None
        items.append({
            "id": f"{shop_id.split('-')[0][:2].upper()}{int(own['sku'][4:]):05d}",
            "own_sku": own["sku"],
            "title": noisy_title(own, r, cfg["noise"]),
            "brand": None if r.random() < cfg["noise"] * 0.35 else own["brand"],
            "category": own["subtype"],
            "color": own["color"] if r.random() > 0.25 else None,
            "price": price, "old_price": old_price,
            "in_stock": r.random() > 0.09,
        })
    # уникальные позиции магазина (нет в нашем каталоге)
    for k in range(cfg["extra_unique"]):
        r = rng_for("uniq", shop_id, k, week)
        if r.random() < 0.15:  # часть уникальных тоже ротируется
            continue
        cat_name = r.choice(list(CATEGORIES))
        subtype = r.choice(CATEGORIES[cat_name])
        brand = r.choice(BRANDS + ["NoName", "Fashion Co", "Trendy"])
        lo, hi = PRICE_RANGE[cat_name]
        day_drift = rng_for("uprice", shop_id, k, day).uniform(-0.1, 0.1)
        price = max(190, round(r.uniform(lo, hi) * (1 + day_drift) / 10) * 10 - 1)
        items.append({
            "id": f"U{shop_id.split('-')[0][:2].upper()}{k:05d}",
            "own_sku": None,
            "title": f"{subtype} {brand} {r.choice(MODEL_WORDS)} {r.randint(1, 999)}",
            "brand": brand, "category": subtype,
            "color": r.choice(COLORS), "price": price, "old_price": None,
            "in_stock": r.random() > 0.1,
        })
    return items


# --------------------------------------------------------------- html render

PAGE_TMPL = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{shop} — каталог, стр. {page}</title><meta name="generator" content="market-sim">
<meta name="run-asof" content="{asof}"></head>
<body><h1>{shop}</h1><nav>{nav}</nav>
<div class="listing">
{cards}
</div></body></html>"""


def render_style_hub(shop_dir, items, cfg, asof, run_rng):
    urls, broken = [], set()
    for it in items:
        if run_rng.random() < cfg["missing_link_rate"]:
            broken.add(it["id"])  # ссылка есть, страницы нет → честный 404
    pages = [items[i:i + cfg["page_size"]] for i in range(0, len(items), cfg["page_size"])]
    for pi, page in enumerate(pages, 1):
        cards, nav = [], []
        for it in page:
            cards.append(f'<div class="product-card" data-sku="{it["id"]}">'
                         f'<a href="p/{it["id"]}.html">{it["title"]}</a>'
                         f'<span class="price" data-value="{it["price"]}">{it["price"]} ₽</span></div>')
        for pj in range(1, len(pages) + 1):
            nav.append(f'<a class="page" href="page{pj}.html">{pj}</a>')
        html = PAGE_TMPL.format(shop="StyleHub", page=pi, asof=iso(asof),
                                nav=" ".join(nav), cards="\n".join(cards))
        _write(shop_dir, f"page{pi}.html", html)
        if pi == 1:
            _write(shop_dir, "index.html", html)
    for it in items:
        if it["id"] in broken:
            continue
        is_broken = run_rng.random() < cfg["broken_rate"]
        price_html = "" if is_broken else \
            f'<div class="buy"><span class="price" itemprop="price">{it["price"]}</span>' + \
            (f'<s class="old-price">{it["old_price"]}</s>' if it["old_price"] else "") + "</div>"
        brand_html = f'<span itemprop="brand">{it["brand"]}</span>' if it["brand"] else ""
        color_html = f'<li>Цвет: <b>{it["color"]}</b></li>' if it["color"] else ""
        html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<title>{it["title"]} — StyleHub</title></head><body itemscope>'
                f'<h1 class="p-title" itemprop="name">{it["title"]}</h1>{brand_html}'
                f'<div class="category">{it["category"]}</div>{price_html}'
                f'<ul class="attrs">{color_html}<li>Наличие: '
                f'{"в наличии" if it["in_stock"] else "нет в наличии"}</li></ul>'
                f'</body></html>')
        _write(shop_dir, f'p/{it["id"]}.html', html)
        urls.append(f'p/{it["id"]}.html')
    # sitemap
    sm = ['<?xml version="1.0" encoding="UTF-8"?>',
          '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        sm.append(f"<url><loc>/shops/style-hub/{u}</loc></url>")
    sm.append("</urlset>")
    _write(shop_dir, "sitemap.xml", "\n".join(sm))


def render_moda_market(shop_dir, items, cfg, asof, run_rng):
    pages = [items[i:i + cfg["page_size"]] for i in range(0, len(items), cfg["page_size"])]
    for pi, page in enumerate(pages, 1):
        rows = []
        for it in page:
            rows.append(f'<tr class="item"><td><a class="lnk" href="item_{it["id"]}.html">'
                        f'{it["title"]}</a></td><td class="cost">{_ru_price(it["price"])}</td></tr>')
        nxt = f'<a rel="next" href="cat_p{pi + 1}.html">Дальше →</a>' if pi < len(pages) else ""
        html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<title>МодаМаркет</title><meta name="run-asof" content="{iso(asof)}"></head>'
                f'<body><h2>МодаМаркет — все товары (стр {pi}/{len(pages)})</h2>'
                f'<table class="goods">{"".join(rows)}</table>{nxt}</body></html>')
        _write(shop_dir, f"cat_p{pi}.html", html)
        if pi == 1:
            _write(shop_dir, "index.html", html)
    for it in items:
        if run_rng.random() < cfg["missing_link_rate"]:
            continue  # честный 404
        if run_rng.random() < cfg.get("captcha_rate", 0):
            _write(shop_dir, f'item_{it["id"]}.html',
                   '<!doctype html><html><body><div id="captcha">Подтвердите, что вы не робот'
                   '</div></body></html>')
            continue
        is_broken = run_rng.random() < cfg["broken_rate"]
        cost = "" if is_broken else f'<div class="cost">Цена: {_ru_price(it["price"])} руб.</div>'
        old = f'<div class="oldcost">Было: {_ru_price(it["old_price"])} руб.</div>' if it["old_price"] and not is_broken else ""
        brand = f'<div class="maker">Производитель: {it["brand"]}</div>' if it["brand"] else ""
        stock = "Есть на складе" if it["in_stock"] else "Раскупили"
        html = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
                f'<title>{it["title"]}</title></head><body>'
                f'<div class="prod"><h3>{it["title"]}</h3>{brand}'
                f'<div class="cat">Раздел: {it["category"]}</div>{cost}{old}'
                f'<div class="stock">{stock}</div></div></body></html>')
        _write(shop_dir, f'item_{it["id"]}.html', html)


def render_trend_api(shop_dir, items, cfg, asof, run_rng):
    pages = [items[i:i + cfg["page_size"]] for i in range(0, len(items), cfg["page_size"])]
    save_json(os.path.join(shop_dir, "index.json"),
              {"shop": "TrendAPI", "asof": iso(asof), "pages": len(pages),
               "page_url": "products_{n}.json"})
    for pi, page in enumerate(pages, 1):
        body = {"page": pi, "total_pages": len(pages), "items": [
            {"id": it["id"], "name": it["title"], "brand": it["brand"],
             "cat": it["category"], "price": it["price"],
             "price_old": it["old_price"], "color": it["color"],
             "available": it["in_stock"]}
            for it in page]}
        path = os.path.join(shop_dir, f"products_{pi}.json")
        save_json(path, body, compact=True)
        if run_rng.random() < cfg["broken_rate"]:  # редкая порча ответа API
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"corrupt": tru')


def _ru_price(p):
    return f"{p:,}".replace(",", " ")


def _write(base, rel, content):
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


MAINTENANCE = ('<!doctype html><html><head><meta charset="utf-8"><title>503</title></head>'
               '<body><h1>Технические работы</h1><p>Сайт временно недоступен.</p></body></html>')


# --------------------------------------------------------------------- main

def generate_market(asof=None):
    asof = asof or datetime.now(timezone.utc)
    catalog = ensure_own_catalog()
    ground_truth, stats = {}, {}
    day = asof.strftime("%Y-%m-%d %H")
    for shop_id, cfg in SHOPS.items():
        shop_dir = os.path.join(SHOPS_DIR, shop_id)
        shutil.rmtree(shop_dir, ignore_errors=True)
        run_rng = rng_for("run", shop_id, day)
        outage = rng_for("outage", shop_id, day).random() < cfg["outage_rate"]
        items = build_shop_assortment(shop_id, cfg, catalog, asof)
        for it in items:
            ground_truth[f"{shop_id}:{it['id']}"] = it["own_sku"]
        if outage:
            _write(shop_dir, "index.html", MAINTENANCE)
            if cfg["kind"] == "json-api":
                _write(shop_dir, "index.json", '{"error": "maintenance"}')
        elif cfg["kind"] == "html-sitemap":
            render_style_hub(shop_dir, items, cfg, asof, run_rng)
        elif cfg["kind"] == "html-pagination":
            render_moda_market(shop_dir, items, cfg, asof, run_rng)
        else:
            render_trend_api(shop_dir, items, cfg, asof, run_rng)
        stats[shop_id] = {"items_true": len(items), "outage": outage,
                          "matched_true": sum(1 for i in items if i["own_sku"])}
    save_json(GROUND_TRUTH_PATH, ground_truth, compact=True)
    _write(SHOPS_DIR, "index.html",
           '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
           '<title>Демо-магазины</title></head><body><h1>Демо-магазины (симуляция рынка)</h1>'
           '<ul><li><a href="style-hub/">StyleHub</a></li>'
           '<li><a href="moda-market/">МодаМаркет</a></li>'
           '<li><a href="trend-api/index.json">TrendAPI (JSON)</a></li></ul></body></html>')
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", help="ISO-время запуска (для бэкфилла)")
    args = ap.parse_args()
    asof = datetime.fromisoformat(args.asof).replace(tzinfo=timezone.utc) if args.asof else None
    print(generate_market(asof))
