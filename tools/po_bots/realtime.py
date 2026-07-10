"""Реалтайм-вахта PO-ботов: непрерывный long polling Telegram.

Один запуск — одна «смена» длиной до N минут внутри GitHub Actions job.
Каждый бот слушает свой поток getUpdates(timeout=45) и отвечает за секунды.
Следующая смена стоит в очереди concurrency-группы воркфлоу и подхватывает
вахту сразу после завершения текущей.

Офсеты апдейтов передаются между сменами через actions/cache
(файл OFFSETS_FILE); при холодном старте хвост истории старше 90 секунд
пропускается, чтобы не отвечать на давно прочитанное дважды.
"""
import argparse
import json
import os
import threading
import time

from tools.po_bots.bot import (PRODUCTS, api, handle_update, load_state,
                               save_state, token_for)

OFFSETS_FILE = os.environ.get("OFFSETS_FILE", "/tmp/bot-offsets.json")
_lock = threading.Lock()


def _load_offsets():
    try:
        with open(OFFSETS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_offsets(offsets):
    with _lock:
        with open(OFFSETS_FILE, "w") as f:
            json.dump(offsets, f)


COLD_START_WINDOW_S = 600  # холодный старт: отвечаем на всё моложе 10 минут


def _cold_start_offset(tok, product):
    """Смена без наследства офсетов: обрабатываем недавнее, пропускаем старьё."""
    r = api(tok, "getUpdates", timeout=0)
    results = r.get("result") or []
    if not results:
        return None
    cutoff = time.time() - COLD_START_WINDOW_S
    for upd in results:
        msg = upd.get("message") or {}
        if msg.get("date", 0) >= cutoff:
            return upd["update_id"]  # с первого свежего — и всё после него
    return results[-1]["update_id"] + 1  # всё старое — пропускаем целиком


def shift(product, deadline, state, offsets):
    tok = token_for(product)
    if not tok:
        print(f"{product}: токен не задан — поток не стартует", flush=True)
        return
    me = api(tok, "getMe")
    if not me.get("ok"):
        print(f"{product}: getMe ОШИБКА — {me.get('description')}", flush=True)
        return
    my = (me.get("result") or {}).get("username", "")
    offset = offsets.get(product) or _cold_start_offset(tok, product)
    print(f"{product}: @{my} на вахте, offset={offset}", flush=True)
    errors = 0
    while time.time() < deadline:
        r = api(tok, "getUpdates", timeout=45, allowed_updates=["message"],
                **({"offset": offset} if offset is not None else {}))
        if not r.get("ok"):
            errors += 1
            print(f"{product}: getUpdates ошибка ({r.get('description')}), "
                  f"пауза 5с", flush=True)
            time.sleep(5 if errors < 10 else 30)
            continue
        errors = 0
        for upd in r.get("result", []):
            offset = upd["update_id"] + 1
            with _lock:
                replied = handle_update(product, my, upd, state)
            if replied:
                text = ((upd.get("message") or {}).get("text") or "")[:60]
                print(f'{product}: ответил на «{text}»', flush=True)
        offsets[product] = offset
        _save_offsets(offsets)
    print(f"{product}: смена окончена", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=340)
    args = ap.parse_args()
    deadline = time.time() + args.minutes * 60
    state = load_state()
    chat_before = state.get("chat_id")
    offsets = _load_offsets()
    threads = [threading.Thread(target=shift, args=(p, deadline, state, offsets),
                                daemon=True)
               for p in PRODUCTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if state.get("chat_id") != chat_before:
        save_state(state)  # новый чат — воркфлоу закоммитит
    print("вахта завершена, передаю смену", flush=True)


if __name__ == "__main__":
    main()
