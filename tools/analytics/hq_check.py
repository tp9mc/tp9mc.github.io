"""Проверка прямой связи с головной компанией (lamoda.ru).

Каждый прогон пайплайн делает один вежливый запрос к www.lamoda.ru и
фиксирует доступность, HTTP-статус и задержку. Любой HTTP-ответ (включая
403 от анти-бота) означает «связь есть»; ошибка сети/DNS — «связи нет».
Из локальной песочницы внешний доступ закрыт — статус будет sandbox.
"""
import os
import time

import requests

from tools.common.util import DATA_DIR, save_json, iso, utcnow

HQ_URL = "https://www.lamoda.ru/"
HQ_PATH = os.path.join(DATA_DIR, "shared", "hq.json")
UA = ("Mozilla/5.0 (compatible; LamodaTech-Cockpit/1.0; "
      "+https://tp9mc.github.io) internal-monitoring")


def check_hq(asof=None):
    asof = asof or utcnow()
    t0 = time.perf_counter()
    try:
        r = requests.get(HQ_URL, timeout=12, headers={"User-Agent": UA},
                         allow_redirects=True)
        result = {"ts": iso(asof), "url": HQ_URL, "reachable": True,
                  "status": r.status_code,
                  "latency_ms": round((time.perf_counter() - t0) * 1000)}
    except requests.RequestException as e:
        blocked_by_sandbox = "CONNECT" in str(e) or "403" in str(e) \
            or type(e).__name__ == "ProxyError"
        result = {"ts": iso(asof), "url": HQ_URL, "reachable": False,
                  "error": "sandbox" if blocked_by_sandbox else type(e).__name__,
                  "latency_ms": round((time.perf_counter() - t0) * 1000)}
    save_json(HQ_PATH, result)
    return result
