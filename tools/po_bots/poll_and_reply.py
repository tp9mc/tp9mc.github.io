"""Опрос Telegram и ответы PO-ботов на сообщения в групповом чате.

Запускается по крону (GitHub Actions). Каждый бот забирает свои апдейты
через getUpdates, запоминает chat_id группы и отвечает на адресованные ему
вопросы. Офсеты и chat_id — в data/bots/state.json (коммитится workflow).
В лог печатается диагностика по каждому боту — по ней разбираются проблемы
вида «боты молчат» (см. плейбук №8 руководства).
"""
from tools.po_bots.bot import (PRODUCTS, answer, api, is_addressed, load_state,
                               save_state, send, token_for)


def process(product, state, diag):
    tok = token_for(product)
    if not tok:
        diag.append(f"{product}: токен не задан — пропуск")
        return 0
    me = api(tok, "getMe")
    if not me.get("ok"):
        diag.append(f"{product}: getMe ОШИБКА — {me.get('description', 'нет ответа')} "
                    f"(проверь секрет {PRODUCTS[product]['env']})")
        return 0
    my = (me.get("result") or {}).get("username", "")
    offset = state["offsets"].get(product)
    r = api(tok, "getUpdates", timeout=0, allowed_updates=["message"],
            **({"offset": offset} if offset else {}))
    if not r.get("ok"):
        diag.append(f"{product} (@{my}): getUpdates ОШИБКА — {r.get('description')}")
        return 0
    updates = r.get("result", [])
    diag.append(f"{product} (@{my}): апдейтов {len(updates)}")
    replied = 0
    for upd in updates:
        state["offsets"][product] = upd["update_id"] + 1
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        text = msg.get("text") or ""
        if not text or (msg.get("from") or {}).get("is_bot"):
            continue
        if chat.get("type") in ("group", "supergroup"):
            state["chat_id"] = chat["id"]  # запоминаем «наш» чат
        elif chat.get("type") == "private":
            # в личке отвечаем всегда
            send(product, chat["id"], answer(product, text))
            replied += 1
            continue
        reply_to = ((msg.get("reply_to_message") or {}).get("from") or {}).get("username")
        cmd_mine = text.startswith("/") and (("@" + my) in text or "@" not in text)
        if cmd_mine and text.startswith("/start"):
            cfg = PRODUCTS[product]
            send(product, chat["id"],
                 f"{cfg['emoji']} Привет! Я {cfg['persona']}, {cfg['role']}. "
                 f"Дайджесты и алерты по продукту буду присылать сюда сама(м). "
                 f"/help — что умею, /manual — руководство по управлению продуктами.")
            replied += 1
            continue
        if cmd_mine or is_addressed(product, text, my, reply_to):
            send(product, chat["id"], answer(product, text))
            replied += 1
    return replied


def main():
    state = load_state()
    diag = []
    total = sum(process(p, state, diag) for p in PRODUCTS)
    save_state(state)
    for line in diag:
        print(line)
    print(f"ответов отправлено: {total}, chat_id={state.get('chat_id')}")


if __name__ == "__main__":
    main()
