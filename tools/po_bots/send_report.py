"""Отправка отчётов PO-ботов в групповой чат после прогона пайплайна.

  --kind digest  — полные дайджесты обоих PO (раз в день)
  --kind alerts  — только алерты severity serious/critical
  --kind auto    — дайджест, если сегодня ещё не слали; иначе алерты при наличии

chat_id берётся из data/bots/state.json (боты запоминают его, увидев сообщение
в группе) или из env TG_CHAT_ID.
"""
import argparse
import os
from datetime import datetime, timezone

from tools.common.util import DATA_DIR, load_json
from tools.po_bots.bot import (DIGESTS, alert_message, load_state, save_state,
                               send, token_for)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="auto", choices=["digest", "alerts", "auto"])
    args = ap.parse_args()

    state = load_state()
    chat_id = state.get("chat_id") or os.environ.get("TG_CHAT_ID")
    if not chat_id:
        print("chat_id ещё не известен — боты узнают его из первого сообщения в группе")
        return
    if not (token_for("parsing") or token_for("matching")):
        print("токены ботов не заданы — пропускаю")
        return

    summary = load_json(os.path.join(DATA_DIR, "summary.json"))
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    kind = args.kind
    if kind == "auto":
        kind = "digest" if state.get("last_digest") != today else "alerts"

    if kind == "digest":
        for product in ("parsing", "matching"):
            if send(product, chat_id, DIGESTS[product]()):
                print(f"digest {product}: отправлен")
        state["last_digest"] = today
        save_state(state)
    else:
        for product in ("parsing", "matching"):
            mine = [a for a in (summary["alerts_open"] if summary else [])
                    if a["severity"] in ("serious", "critical")
                    and (a["product"] == product
                         or (product == "matching" and a["product"] == "shared"))]
            if mine and send(product, chat_id, alert_message(product, mine)):
                print(f"alerts {product}: {len(mine)} отправлено")


if __name__ == "__main__":
    main()
