"""Разовый опрос Telegram (ручной запасной вариант к realtime-вахте).

Забирает накопившиеся апдейты обоих ботов, отвечает на адресованные
сообщения и сохраняет офсеты/chat_id в data/bots/state.json.
Не запускать параллельно с realtime-bots: getUpdates отдаётся только
одному потребителю (второй получает 409 Conflict).
"""
from tools.po_bots.bot import (PRODUCTS, api, handle_update, load_state,
                               save_state, token_for)


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
        replied += handle_update(product, my, upd, state)
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
