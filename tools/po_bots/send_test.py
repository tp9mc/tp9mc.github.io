"""Ручной тест связи: каждый бот шлёт тестовое сообщение в чат.

Печатает полную диагностику: getMe, getWebhookInfo (webhook блокирует
getUpdates — если он установлен, снимаем), содержимое getUpdates.
chat_id берётся из input воркфлоу → state.json → TG_CHAT_ID → getUpdates.
"""
import json
import os

from tools.po_bots.bot import PRODUCTS, api, load_state, save_state, token_for


def main():
    state = load_state()
    chat_id = (os.environ.get("TEST_CHAT_ID") or state.get("chat_id")
               or os.environ.get("TG_CHAT_ID"))
    sent = 0
    for product, cfg in PRODUCTS.items():
        tok = token_for(product)
        if not tok:
            print(f"{product}: токен не задан")
            continue
        me = api(tok, "getMe")
        print(f'{product} getMe: ok={me.get("ok")} '
              f'{json.dumps(me.get("result") or me.get("description"), ensure_ascii=False)}')
        wh = (api(tok, "getWebhookInfo").get("result")) or {}
        print(f'{product} webhook: url="{wh.get("url", "")}" '
              f'pending={wh.get("pending_update_count", "?")}')
        if wh.get("url"):
            print(f'{product} webhook мешает getUpdates — снимаю: '
                  f'{api(tok, "deleteWebhook")}')
        upd = api(tok, "getUpdates", timeout=0)
        results = upd.get("result", [])
        print(f'{product} getUpdates: ok={upd.get("ok")} count={len(results)} '
              f'{upd.get("description", "")}')
        for u in results[:6]:
            msg = u.get("message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            text = (msg.get("text") or "")[:40]
            print(f'{product}   upd {u.get("update_id")}: chat={chat.get("type")} '
                  f'{chat.get("id")} text="{text}"')
            if chat.get("type") in ("group", "supergroup") and not chat_id:
                chat_id = chat["id"]
        if chat_id:
            r = api(tok, "sendMessage", chat_id=int(chat_id),
                    text=f'{cfg["emoji"]} Тест связи: {cfg["persona"]} '
                         f'({cfg["role"]}) в эфире. Приём!')
            print(f'{product} sendMessage: ok={r.get("ok")} {r.get("description", "")}')
            sent += 1 if r.get("ok") else 0
        else:
            print(f"{product}: chat_id неизвестен — отправить не могу "
                  f"(укажи chat_id при запуске воркфлоу или дождись /start в группе)")
    if chat_id and not state.get("chat_id"):
        state["chat_id"] = int(chat_id)
        save_state(state)
        print(f"chat_id={chat_id} сохранён в состояние")
    print(f"итого отправлено: {sent}")


if __name__ == "__main__":
    main()
