"""Опрос Telegram и ответы PO-ботов на сообщения в групповом чате.

Запускается по крону (GitHub Actions). Каждый бот забирает свои апдейты
через getUpdates, запоминает chat_id группы и отвечает на адресованные ему
вопросы. Офсеты и chat_id — в data/bots/state.json (коммитится workflow).
"""
from tools.po_bots.bot import (PRODUCTS, answer, api, is_addressed, load_state,
                               save_state, send, token_for)


def me_username(token, cache={}):
    if token not in cache:
        r = api(token, "getMe")
        cache[token] = (r.get("result") or {}).get("username", "")
    return cache[token]


def process(product, state):
    tok = token_for(product)
    if not tok:
        return 0
    my = me_username(tok)
    offset = state["offsets"].get(product)
    r = api(tok, "getUpdates", timeout=0, allowed_updates=["message"],
            **({"offset": offset} if offset else {}))
    if not r.get("ok"):
        return 0
    replied = 0
    for upd in r.get("result", []):
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
    total = sum(process(p, state) for p in PRODUCTS)
    save_state(state)
    print(f"ответов отправлено: {total}, chat_id={state.get('chat_id')}")


if __name__ == "__main__":
    main()
