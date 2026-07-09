"""PO-боты: два продакт-оунера, которые живут в групповом Telegram-чате.

  * Лера — PO LT Parsing  (токен в env PARSING_BOT_TOKEN)
  * Марк — PO LT Matching (токен в env MATCHING_BOT_TOKEN)

Боты шлют дайджесты/алерты после прогонов пайплайна и отвечают на вопросы
Head of Product. Общее состояние (chat_id, офсеты) — в data/bots/state.json.
"""
import os
import re

import requests

from tools.common.util import DATA_DIR, load_json, save_json

STATE_PATH = os.path.join(DATA_DIR, "bots", "state.json")
API = "https://api.telegram.org/bot{token}/{method}"

PRODUCTS = {
    "parsing": {
        "env": "PARSING_BOT_TOKEN", "persona": "Лера", "role": "PO LT Parsing",
        "emoji": "🕷", "sign": "— Лера, PO LT Parsing",
    },
    "matching": {
        "env": "MATCHING_BOT_TOKEN", "persona": "Марк", "role": "PO LT Matching",
        "emoji": "🧩", "sign": "— Марк, PO LT Matching",
    },
}


def token_for(product):
    return os.environ.get(PRODUCTS[product]["env"], "")


def load_state():
    return load_json(STATE_PATH, default={"chat_id": None, "offsets": {},
                                          "last_digest": None})


def save_state(state):
    save_json(STATE_PATH, state)


def api(token, method, **params):
    try:
        r = requests.post(API.format(token=token, method=method),
                          json=params, timeout=30)
        return r.json()
    except requests.RequestException:
        return {"ok": False}


def send(product, chat_id, text):
    tok = token_for(product)
    if not tok or not chat_id:
        return False
    resp = api(tok, "sendMessage", chat_id=chat_id, text=text,
               parse_mode="HTML", disable_web_page_preview=True)
    return resp.get("ok", False)


# ---------------------------------------------------------------- formatting

def pct(v):
    return f"{v * 100:.1f}%".replace(".0%", "%")


def trend(hist, key, fmt=pct, invert=False):
    if len(hist) < 2:
        return ""
    a, b = hist[-2].get(key), hist[-1].get(key)
    if a is None or b is None or a == b:
        return ""
    good = (b > a) != invert
    return f' ({"↗" if b > a else "↘"}{fmt(abs(b - a))}, {"хорошо" if good else "следим"})'


def _load_all():
    return {
        "summary": load_json(os.path.join(DATA_DIR, "summary.json")),
        "pl": load_json(os.path.join(DATA_DIR, "parsing", "latest.json")),
        "ph": load_json(os.path.join(DATA_DIR, "parsing", "history.json"), default=[]),
        "ml": load_json(os.path.join(DATA_DIR, "matching", "latest.json")),
        "mh": load_json(os.path.join(DATA_DIR, "matching", "history.json"), default=[]),
        "pi": load_json(os.path.join(DATA_DIR, "matching", "price_index.json")),
        "alerts": load_json(os.path.join(DATA_DIR, "shared", "alerts.json"), default=[]),
        "hq": load_json(os.path.join(DATA_DIR, "shared", "hq.json")),
    }


def digest_parsing(d=None):
    d = d or _load_all()
    pl, ph, s = d["pl"], d["ph"], d["summary"]
    if not pl:
        return "Данных прогона ещё нет."
    src_lines = []
    for src, v in pl["sources"].items():
        st = "✅" if v["ok"] else ("🛠 maintenance" if v["outage"] else "🔴 сбой")
        src_lines.append(f"  · {src}: {st}, офферов {v['offers']}, success {pct(v['success_rate'])}")
    my_alerts = [a for a in (s["alerts_open"] if s else []) if a["product"] == "parsing"]
    risks = "\n".join(f"⚠️ {a['text']}" for a in my_alerts) or "Рисков по продукту нет."
    return (f"🕷 <b>LT Parsing — дайджест прогона {pl['run_id']}</b>\n\n"
            f"Собрано <b>{pl['coverage']['offers_parsed']}</b> офферов из "
            f"{pl['coverage']['sources_active']}/{pl['coverage']['sources_total']} источников"
            f"{trend(ph, 'offers', lambda v: str(round(v)))}.\n"
            f"Success rate {pct(pl['reliability']['crawl_success_rate'])}"
            f"{trend(ph, 'success')}, свежесть в SLA {pct(pl['freshness']['fresh_share'])}.\n"
            f"Рынок: {pl['delta']['price_changes']} изменений цен, "
            f"+{pl['delta']['new_offers']}/-{pl['delta']['removed_offers']} офферов, "
            f"OOS {pct(pl['delta']['oos_share'])}.\n\n"
            f"Источники:\n" + "\n".join(src_lines) + f"\n\n{risks}\n\n"
            f"<i>{PRODUCTS['parsing']['sign']}</i>")


def digest_matching(d=None):
    d = d or _load_all()
    ml, mh, s = d["ml"], d["mh"], d["summary"]
    if not ml:
        return "Данных прогона ещё нет."
    q = ml["quality"]
    my_alerts = [a for a in (s["alerts_open"] if s else []) if a["product"] in ("matching", "shared")]
    risks = "\n".join(f"⚠️ {a['text']}" for a in my_alerts) or "Рисков по продукту нет."
    pairs = f"{ml['input']['pairs_scored']:,}".replace(",", " ")
    return (f"🧩 <b>LT Matching — дайджест прогона {ml['run_id']}</b>\n\n"
            f"Вход: {ml['input']['offers_in']} офферов, оценено {pairs} пар.\n"
            f"Авто-мэтч {ml['funnel']['auto']} ({pct(ml['funnel']['auto_match_rate'])})"
            f"{trend(mh, 'auto_rate')}, итоговый match rate {pct(ml['funnel']['total_match_rate'])}.\n"
            f"Качество: precision <b>{pct(q['precision_auto'])}</b>{trend(mh, 'precision')}, "
            f"recall {pct(q['recall'])}{trend(mh, 'recall')}, F1 {pct(q['f1'])}.\n"
            f"Очередь валидации: {ml['review']['queue_size']} карточек "
            f"(разобрали {ml['review']['processed']} за прогон).\n\n"
            f"💰 Ценовой индекс: покрытие {pct(ml['price_index']['coverage'])}, "
            f"наша цена лучшая у {pct(ml['price_index']['competitiveness'])} SKU, "
            f"средний разрыв {ml['price_index']['avg_gap_pct']:+.1f}%.\n\n"
            f"{risks}\n\n<i>{PRODUCTS['matching']['sign']}</i>")


DIGESTS = {"parsing": digest_parsing, "matching": digest_matching}


def alert_message(product, alerts):
    cfg = PRODUCTS[product]
    sev_emoji = {"warning": "⚠️", "serious": "🟠", "critical": "🔴"}
    lines = "\n".join(f"{sev_emoji.get(a['severity'], '⚠️')} {a['text']}" for a in alerts)
    return (f"{cfg['emoji']} <b>Алерты по {cfg['role'].split(' ', 1)[1]}</b>\n{lines}\n"
            f"Разбираюсь; вернусь с апдейтом после следующего прогона.\n"
            f"<i>{cfg['sign']}</i>")


# ------------------------------------------------------------------- Q&A

MANUAL_URL = "https://tp9mc.github.io/manual.html"

MANUAL = {
    "parsing": (
        "📖 <b>Шпаргалка по LT Parsing</b>\n\n"
        "Продукт отвечает на вопрос: «видим ли мы рынок целиком, быстро и без потерь качества».\n\n"
        "Светофор:\n"
        "· источники 3/3, success rate ≥ 97%, свежесть 100% в SLA 6 ч, полнота цены ≥ 95% — норма\n"
        "· maintenance источника — ждать 1–2 прогона, восстановится само\n"
        "· сбой источника 2+ прогона подряд — смотреть лог Actions → pipeline, чинить адаптер\n"
        "· капчи &gt;10 и растут — снижать агрессивность обкачки\n\n"
        f"Полное руководство пилота (плейбуки, пороги, ручки): {MANUAL_URL}\n"
        "Спросить меня: статус · источники · свежесть · покрытие · качество данных · риски"
    ),
    "matching": (
        "📖 <b>Шпаргалка по LT Matching</b>\n\n"
        "Продукт отвечает на вопрос: «про какую долю каталога знаем цены конкурентов — и можно ли этому верить».\n\n"
        "Светофор:\n"
        "· precision ≥ 95%, recall ≥ 80%, очередь &lt; 150 — норма\n"
        "· precision &lt; 95% — сначала проверить полноту полей у Parsing, потом порог AUTO_T\n"
        "· precision &lt; 90% — авто-мэтчам не верить, ценовые решения на паузу\n"
        "· очередь растёт 5+ прогонов — поднять REVIEW_CAPACITY или улучшать скоринг\n"
        "· покрытие индекса ↓ — идти по конвейеру против течения: источники → офферы → auto-rate → очередь\n\n"
        f"Полное руководство пилота (плейбуки, пороги, ручки): {MANUAL_URL}\n"
        "Спросить меня: статус · качество · очередь · прайс-индекс · цены конкурентов · риски"
    ),
}

HELP = {
    "parsing": ("Я отвечаю за сбор данных конкурентов. Спросите меня про: "
                "<b>статус</b>, <b>источники</b>, <b>покрытие</b>, <b>свежесть</b>, "
                "<b>качество данных</b>, <b>риски</b> — или командой /status, /digest, /risks. "
                f"Инструкция по управлению: /manual или {MANUAL_URL}"),
    "matching": ("Я отвечаю за сопоставление товаров и ценовой индекс. Спросите меня про: "
                 "<b>статус</b>, <b>качество</b> (precision/recall), <b>очередь</b>, "
                 "<b>прайс-индекс</b>, <b>конкурентов по ценам</b>, <b>риски</b> — "
                 "или командой /status, /digest, /risks. "
                 f"Инструкция по управлению: /manual или {MANUAL_URL}"),
}


def answer(product, text):
    """Ответ PO на вопрос в чате. Ключевые слова → готовые срезы данных."""
    d = _load_all()
    t = text.lower()
    cfg = PRODUCTS[product]

    def has(*words):
        return any(w in t for w in words)

    if has("/manual", "инструкци", "руковод", "мануал", "плейбук", "как управлять"):
        return MANUAL[product]
    if has("lamoda", "ламода", "головн"):
        hq = d["hq"]
        if not hq:
            return "Проверка связи с lamoda.ru ещё не выполнялась — будет в следующем прогоне."
        if hq["reachable"]:
            return (f"Связь с головной компанией: <b>online</b> — "
                    f"https://www.lamoda.ru отвечает (HTTP {hq['status']}, "
                    f"{hq['latency_ms']} мс). Проверяем каждый прогон.\n<i>{cfg['sign']}</i>")
        note = ("запуск был из локальной песочницы без внешнего доступа"
                if hq.get("error") == "sandbox" else f"ошибка сети: {hq.get('error')}")
        return (f"Связь с головной компанией: <b>нет ответа</b> ({note}). "
                f"Последняя проверка {hq['ts']}.\n<i>{cfg['sign']}</i>")
    if has("/help", "что ты умеешь", "помощь"):
        return HELP[product]
    if has("/digest", "дайджест", "сводка", "отчет", "отчёт"):
        return DIGESTS[product](d)
    if has("/risks", "риск", "проблем", "алерт", "инцидент"):
        s = d["summary"]
        mine = [a for a in (s["alerts_open"] if s else [])
                if a["product"] == product or a["product"] == "shared"]
        if not mine:
            return f"Открытых рисков по продукту нет. {cfg['emoji']}\n<i>{cfg['sign']}</i>"
        return alert_message(product, mine)

    if product == "parsing":
        pl = d["pl"]
        if not pl:
            return "Прогонов ещё не было."
        if has("источник", "магазин", "shop"):
            rows = [f"· {src}: {'ок' if v['ok'] else 'недоступен'}, {v['offers']} офферов, "
                    f"задержка p95 {v['p95_latency_ms']} мс"
                    for src, v in pl["sources"].items()]
            return "Источники сейчас:\n" + "\n".join(rows) + f"\n<i>{cfg['sign']}</i>"
        if has("свежест", "sla", "актуальн"):
            ages = ", ".join(f"{k}: {v} ч" if v is not None else f"{k}: нет данных"
                             for k, v in pl["freshness"]["age_hours"].items())
            return (f"Свежесть данных (возраст последней успешной обкачки): {ages}. "
                    f"SLA — {pl['freshness']['sla_hours']} ч, в SLA "
                    f"{pct(pl['freshness']['fresh_share'])} источников.\n<i>{cfg['sign']}</i>")
        if has("покрыти", "coverage", "объем", "объём", "сколько"):
            return (f"За последний прогон собрали {pl['coverage']['offers_parsed']} офферов, "
                    f"{pl['coverage']['categories_covered']} категорий, "
                    f"{pl['coverage']['sources_active']}/{pl['coverage']['sources_total']} источников. "
                    f"Покрытие рынка — {pct(d['summary']['nsm']['market_coverage'])}.\n<i>{cfg['sign']}</i>")
        if has("качество", "полнота", "ошибк"):
            fc = pl["quality"]["field_completeness"]
            return (f"Полнота полей: цена {pct(fc['price'])}, бренд {pct(fc['brand'])}, "
                    f"категория {pct(fc['category'])}. Success rate {pct(pl['reliability']['crawl_success_rate'])}, "
                    f"капч поймали {pl['reliability']['blocked']}.\n<i>{cfg['sign']}</i>")
        if has("/status", "статус", "как дела", "здоров"):
            return digest_parsing(d)
    else:
        ml = d["ml"]
        if not ml:
            return "Прогонов ещё не было."
        if has("очеред", "валидаци", "ревью", "разбор"):
            r = ml["review"]
            return (f"Очередь валидации: {r['queue_size']} карточек, за прогон разбираем "
                    f"{r['capacity_per_run']}. Прогноз разбора — {r['backlog_eta_runs']} прогонов. "
                    f"Точность валидаторов {pct(ml['quality']['review_accuracy'])}.\n<i>{cfg['sign']}</i>")
        if has("прайс", "цен", "индекс", "конкурент"):
            p = ml["price_index"]
            top = d["pi"]["rows"][0] if d["pi"] and d["pi"]["rows"] else None
            extra = (f" Максимальный разрыв: {top['title']} — наша {top['own']} ₽ против "
                     f"{top['min_comp']} ₽ ({top['gap_pct']:+.1f}%).") if top else ""
            return (f"Ценовой индекс: покрытие {pct(p['coverage'])}, мы дешевле или в паритете "
                    f"у {pct(p['competitiveness'])} сопоставимых SKU, средний разрыв "
                    f"{p['avg_gap_pct']:+.1f}%.{extra}\n<i>{cfg['sign']}</i>")
        if has("качество", "precision", "recall", "точност", "полнот"):
            q = ml["quality"]
            return (f"Качество против голдсета ({q['golden_set_size']} пар): "
                    f"precision {pct(q['precision_auto'])}, recall {pct(q['recall'])}, "
                    f"F1 {pct(q['f1'])}, ложные мэтчи {pct(q['false_match_rate'])}.\n<i>{cfg['sign']}</i>")
        if has("/status", "статус", "как дела", "здоров", "мэтч", "match"):
            return digest_matching(d)

    return (f"Принял, посмотрю. Если нужен срез данных — {HELP[product]}")


def is_addressed(product, text, my_username, reply_to_username=None):
    """Кому адресовано сообщение: явное упоминание, имя персоны, ключевые слова продукта."""
    t = text.lower()
    cfg = PRODUCTS[product]
    if my_username and ("@" + my_username.lower()) in t:
        return True
    if reply_to_username and my_username and reply_to_username.lower() == my_username.lower():
        return True
    if cfg["persona"].lower() in t:
        return True
    kw = {
        "parsing": ("парсинг", "источник", "обкачк", "краулер", "свежест", "магазин", "capч", "капч"),
        "matching": ("мэтчинг", "матчинг", "сопоставл", "очеред", "прайс", "индекс",
                     "precision", "recall", "валидаци", "цен"),
    }
    return any(w in t for w in kw[product])
