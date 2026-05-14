"""
Send the night work report via Telegram bot to OWNER_CHAT_ID.
"""
import json
import os
import sys
from pathlib import Path

import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import BOT_TOKEN, OWNER_CHAT_ID  # noqa: E402

API = f'https://api.telegram.org/bot{BOT_TOKEN}'


def send_text(chat_id, text, parse_mode=None):
    r = requests.post(f'{API}/sendMessage', json={
        'chat_id': chat_id, 'text': text,
        'parse_mode': parse_mode, 'disable_web_page_preview': True,
    }, timeout=30)
    r.raise_for_status()
    return r.json()


def send_doc(chat_id, path, caption=None):
    with open(path, 'rb') as f:
        r = requests.post(f'{API}/sendDocument',
            data={'chat_id': chat_id, 'caption': caption or ''},
            files={'document': f}, timeout=60)
    r.raise_for_status()
    return r.json()


def main():
    report_text = (REPO / 'NIGHT_REPORT_20260514.md').read_text(encoding='utf-8')

    # short summary message (always within 4096 limit)
    summary = sys.argv[1] if len(sys.argv) > 1 else 'Отчёт о ночной работе во вложении.'
    print(send_text(OWNER_CHAT_ID, summary))

    # send the full report as a document
    print(send_doc(OWNER_CHAT_ID, str(REPO / 'NIGHT_REPORT_20260514.md'),
                   caption='Полный отчёт за ночь'))


if __name__ == '__main__':
    main()
