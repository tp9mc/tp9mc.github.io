"""
Interior Constructor Bot
Receives web_app_data from Mini App, generates a room render via SD WebUI,
sends status updates every 30s, delivers final image to user.
"""
import json, threading, time, base64, requests, telebot
from io import BytesIO
from PIL import Image

from bot_secrets import BOT_TOKEN  # not committed to git
SD_URL    = 'http://localhost:7860/sdapi/v1/txt2img'
CATALOG   = '/tmp/catalog.json'
SD_TIMEOUT = 600   # seconds per generation attempt

STYLE_DNA = {
    'japandi':        'japandi interior design, warm oak wood, natural linen fabric, wabi-sabi minimalism, earth tones, zen atmosphere',
    'modern_classic': 'modern classic interior design, Carrara marble surfaces, deep velvet upholstery, polished brass accents, grand symmetry',
    'scandi':         'scandinavian interior design, light birch wood, crisp white walls, soft wool textiles, hygge coziness, natural daylight',
}
ROOM_DNA = {
    'living':   'spacious living room, comfortable seating area, coffee table, ambient lighting, large windows',
    'bedroom':  'elegant bedroom, king-size bed with luxurious pillows, bedside tables, warm soft lighting',
    'bathroom': 'refined bathroom, clean white fixtures, natural stone surfaces, elegant vanity, spa atmosphere',
    'kitchen':  'modern kitchen, functional workspace, clean countertops, organized storage, natural light',
}
STYLE_RU = {
    'japandi':        'Japandi',
    'modern_classic': 'Модерн Классик',
    'scandi':         'Скандинавский',
}
ROOM_RU = {
    'living':   'Гостиная',
    'bedroom':  'Спальня',
    'bathroom': 'Ванная',
    'kitchen':  'Кухня',
}

NEG = ('people, person, human figure, ugly, deformed, noisy, blurry, low resolution, '
       'oversaturated, flat lighting, text, watermark, logo, clutter, dark')


def load_catalog():
    try:
        with open(CATALOG, encoding='utf-8') as f:
            return json.load(f)['items']
    except Exception:
        return {}


def item_keywords(positive: str) -> str:
    """Extract short keyword phrase from a product-shot prompt."""
    # Strip boilerplate render prefixes
    for prefix in [
        'minimalist 3D render of a ', 'minimalist 3D render of an ',
        'minimalist 3D render of ', 'product photo, ',
        'isolated on pure white background', 'isolated on white background',
        'soft studio lighting', 'studio lighting', 'high quality',
        'product photography',
    ]:
        positive = positive.replace(prefix, '')
    # Take first meaningful segment (up to first comma or 60 chars)
    seg = positive.split(',')[0].strip()
    return seg[:80] if seg else ''


def build_prompt(style: str, room: str, setup: dict, catalog: dict) -> str:
    parts = [STYLE_DNA[style], ROOM_DNA[room]]

    # Pull key items from furniture (slots 1–3) and lighting (slot 1)
    priority_slots = [
        ('furniture', 'f_1'), ('furniture', 'f_2'), ('furniture', 'f_3'),
        ('lighting',  'l_1'),
    ]
    try:
        room_cat = catalog[style][room]
        for cat_id, slot_px in priority_slots:
            variant  = setup.get(cat_id, {}).get(slot_px, 'main')
            # non-kitchen key: "1_main" / "1_alt"
            slot_num = slot_px.split('_')[1]
            item = (room_cat[cat_id].get(f'{slot_num}_{variant}') or
                    room_cat[cat_id].get(slot_num) or {})
            kw = item_keywords(item.get('positive', ''))
            if kw:
                parts.append(kw)
    except (KeyError, TypeError):
        pass

    parts += [
        'professional interior photography',
        'natural daylight', 'wide angle lens', 'high quality',
        'architectural digest', 'photo realistic', '8k',
    ]
    return ', '.join(p for p in parts if p)


def generate_room(chat_id: int, payload: dict, bot: telebot.TeleBot):
    style = payload.get('style', '')
    room  = payload.get('room', '')
    setup = payload.get('setup', {})

    if style not in STYLE_DNA or room not in ROOM_DNA:
        bot.send_message(chat_id, '❌ Неизвестный стиль или комната.')
        return

    catalog = load_catalog()
    prompt  = build_prompt(style, room, setup, catalog)

    bot.send_message(
        chat_id,
        f'🏠 Генерирую *{STYLE_RU[style]}* · *{ROOM_RU[room]}*\n'
        f'Это займёт 1–3 минуты, буду присылать обновления.',
        parse_mode='Markdown',
    )

    # Status ticker — every 30s until stop_event is set
    stop_event = threading.Event()
    start_ts   = time.time()

    def ticker():
        tick = 0
        while not stop_event.wait(30):
            tick += 1
            elapsed = int(time.time() - start_ts)
            bot.send_message(
                chat_id,
                f'⏳ Генерация идёт… {elapsed // 60}м {elapsed % 60:02d}с',
            )

    t = threading.Thread(target=ticker, daemon=True)
    t.start()

    try:
        r = requests.post(SD_URL, json={
            'prompt':          prompt,
            'negative_prompt': NEG,
            'steps':           30,
            'cfg_scale':       7,
            'width':           1024,
            'height':          576,
            'sampler_name':    'Euler',
            'scheduler':       'Simple',
            'seed':            -1,
            'override_settings': {'CLIP_stop_at_last_layers': 1},
        }, timeout=SD_TIMEOUT)

        stop_event.set()

        if r.status_code != 200:
            bot.send_message(chat_id, f'❌ SD WebUI вернул HTTP {r.status_code}')
            return

        img_bytes = base64.b64decode(r.json()['images'][0])
        img = Image.open(BytesIO(img_bytes)).convert('RGB')
        buf = BytesIO()
        img.save(buf, 'JPEG', quality=92)
        buf.seek(0)

        elapsed = int(time.time() - start_ts)
        bot.send_photo(
            chat_id, buf,
            caption=(
                f'✅ Готово за {elapsed // 60}м {elapsed % 60:02d}с\n'
                f'*{STYLE_RU[style]}* · *{ROOM_RU[room]}*'
            ),
            parse_mode='Markdown',
        )

    except requests.Timeout:
        stop_event.set()
        bot.send_message(chat_id, '❌ SD WebUI не ответил за 10 минут — попробуй ещё раз.')
    except Exception as e:
        stop_event.set()
        bot.send_message(chat_id, f'❌ Ошибка: {str(e)[:120]}')


def main():
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

    @bot.message_handler(content_types=['web_app_data'])
    def on_web_app_data(message):
        try:
            payload = json.loads(message.web_app_data.data)
        except Exception:
            bot.send_message(message.chat.id, '❌ Не удалось разобрать данные из Mini App.')
            return
        threading.Thread(
            target=generate_room,
            args=(message.chat.id, payload, bot),
            daemon=True,
        ).start()

    @bot.message_handler(commands=['start'])
    def on_start(message):
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton(
            text='🏠 Открыть конструктор',
            web_app=telebot.types.WebAppInfo(url='https://tp9mc.github.io'),
        ))
        bot.send_message(
            message.chat.id,
            '👋 Привет! Выбери стиль и комнату, нажми «Сгенерировать» — '
            'я создам рендер и пришлю сюда.',
            reply_markup=markup,
        )

    print('Bot started, polling…')
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == '__main__':
    main()
