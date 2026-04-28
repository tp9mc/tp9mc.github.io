"""
Interior Constructor Bot
Receives web_app_data from Mini App, generates a room render via SD WebUI,
sends status updates every 30s, delivers image + full report to user.
"""
import json, threading, time, base64, requests, telebot
from io import BytesIO
from PIL import Image
from datetime import datetime

from bot_secrets import BOT_TOKEN  # not committed to git
SD_URL     = 'http://localhost:7860/sdapi/v1/txt2img'
CATALOG    = '/tmp/catalog.json'
SD_TIMEOUT = 600

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
STYLE_RU = {'japandi': 'Japandi', 'modern_classic': 'Модерн Классик', 'scandi': 'Скандинавский'}
ROOM_RU   = {'living': 'Гостиная', 'bedroom': 'Спальня', 'bathroom': 'Ванная', 'kitchen': 'Кухня'}
CATS      = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm')]
CAT_RU    = {'furniture': 'Мебель', 'lighting': 'Освещение', 'materials': 'Материалы'}

NEG = ('people, person, human figure, ugly, deformed, noisy, blurry, low resolution, '
       'oversaturated, flat lighting, text, watermark, logo, clutter, dark')


def load_catalog():
    try:
        with open(CATALOG, encoding='utf-8') as f:
            return json.load(f)['items']
    except Exception:
        return {}


def strip_boilerplate(text: str) -> str:
    for s in [
        'minimalist 3D render of an ', 'minimalist 3D render of a ',
        'minimalist 3D render of ', 'A highly detailed, photorealistic render of a ',
        'A highly detailed, photorealistic render of an ',
        'isolated on pure white background', 'isolated on white background',
        'soft studio lighting', 'studio lighting', 'product photography',
        'high quality', 'Positive:', 'Positive Prompt:',
    ]:
        text = text.replace(s, '')
    return text.strip(' ,')


def get_item(catalog, style, room, cat_id, slot_num, variant):
    """Look up a catalog item, handling kitchen vs non-kitchen key format."""
    try:
        cat = catalog[style][room][cat_id]
        if room == 'kitchen':
            return cat.get(str(slot_num), {})
        return cat.get(f'{slot_num}_{variant}', {})
    except (KeyError, TypeError):
        return {}


def item_phrase(item: dict) -> str:
    """Short keyword phrase from catalog item for use in scene prompt."""
    pos = item.get('positive', '')
    if not pos:
        return ''
    clean = strip_boilerplate(pos)
    # Take first two comma-segments for enough context
    segs = [s.strip() for s in clean.split(',') if s.strip()]
    return ', '.join(segs[:2])[:100]


def build_prompt_and_report(style, room, setup, catalog):
    """
    Returns (prompt_str, selections_list).
    selections_list: list of (cat_id, slot_num, variant, phrase) for all 27 slots.
    """
    scene_parts  = [STYLE_DNA[style], ROOM_DNA[room]]
    selections   = []  # (cat_id, slot_num, variant, phrase)

    for cat_id, px in CATS:
        cat_setup = setup.get(cat_id, {})
        for n in range(1, 10):
            slot_key = f'{px}_{n}'
            variant  = cat_setup.get(slot_key, 'main')
            item     = get_item(catalog, style, room, cat_id, n, variant)
            phrase   = item_phrase(item)
            selections.append((cat_id, n, variant, phrase))
            if phrase:
                scene_parts.append(phrase)

    scene_parts += [
        'professional interior photography', 'natural daylight',
        'wide angle lens', 'high quality', 'architectural digest',
        'photo realistic', '8k',
    ]
    prompt = ', '.join(p for p in scene_parts if p)
    return prompt, selections


def build_report(style, room, setup, selections, prompt, seed, elapsed_sec):
    lines = []
    lines.append('=' * 60)
    lines.append('  INTERIOR CONSTRUCTOR — GENERATION REPORT')
    lines.append('=' * 60)
    lines.append(f'Date:    {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'Style:   {STYLE_RU[style]} ({style})')
    lines.append(f'Room:    {ROOM_RU[room]} ({room})')
    lines.append(f'Time:    {elapsed_sec // 60}m {elapsed_sec % 60:02d}s')
    lines.append('')
    lines.append('─' * 60)
    lines.append('  ВЫБОРЫ ПОЛЬЗОВАТЕЛЯ (все 27 слотов)')
    lines.append('─' * 60)

    for cat_id, px in CATS:
        lines.append(f'\n[{CAT_RU[cat_id]}]')
        cat_sels = [(n, v, ph) for (c, n, v, ph) in selections if c == cat_id]
        for n, variant, phrase in cat_sels:
            px_id = [p for _, p in CATS if _ == cat_id][0]
            slot_label = f'{px_id}_{n}'
            flag = '→' if variant == 'main' else '⇒'
            lines.append(f'  {slot_label:5s} [{variant:4s}] {flag} {phrase or "(нет в каталоге)"}')

    lines.append('')
    lines.append('─' * 60)
    lines.append('  ПАРАМЕТРЫ ГЕНЕРАЦИИ')
    lines.append('─' * 60)
    lines.append('Model:     Juggernaut XL v9 (juggernautXL_v9Rdphoto2Lightning.safetensors)')
    lines.append('Sampler:   Euler')
    lines.append('Scheduler: Simple')
    lines.append('Steps:     30')
    lines.append('CFG Scale: 7')
    lines.append(f'Seed:      {seed}')
    lines.append('Size:      1024x576')
    lines.append('')
    lines.append('─' * 60)
    lines.append('  ПОЛНЫЙ ПРОМТ')
    lines.append('─' * 60)
    lines.append(prompt)
    lines.append('')
    lines.append('─' * 60)
    lines.append('  НЕГАТИВНЫЙ ПРОМТ')
    lines.append('─' * 60)
    lines.append(NEG)
    lines.append('')
    lines.append('=' * 60)
    return '\n'.join(lines)


def generate_room(chat_id: int, payload: dict, bot: telebot.TeleBot):
    style = payload.get('style', '')
    room  = payload.get('room', '')
    setup = payload.get('setup', {})

    if style not in STYLE_DNA or room not in ROOM_DNA:
        bot.send_message(chat_id, '❌ Неизвестный стиль или комната.')
        return

    catalog    = load_catalog()
    prompt, selections = build_prompt_and_report(style, room, setup, catalog)

    bot.send_message(
        chat_id,
        f'🏠 Генерирую *{STYLE_RU[style]}* · *{ROOM_RU[room]}*\n'
        f'Это займёт 1–3 минуты, буду присылать обновления.',
        parse_mode='Markdown',
    )

    stop_event = threading.Event()
    start_ts   = time.time()

    def ticker():
        while not stop_event.wait(30):
            elapsed = int(time.time() - start_ts)
            bot.send_message(chat_id, f'⏳ Генерация идёт… {elapsed // 60}м {elapsed % 60:02d}с')

    threading.Thread(target=ticker, daemon=True).start()

    try:
        r = requests.post(SD_URL, json={
            'prompt':            prompt,
            'negative_prompt':   NEG,
            'steps':             30,
            'cfg_scale':         7,
            'width':             1024,
            'height':            576,
            'sampler_name':      'Euler',
            'scheduler':         'Simple',
            'seed':              -1,
            'override_settings': {'CLIP_stop_at_last_layers': 1},
        }, timeout=SD_TIMEOUT)

        stop_event.set()

        if r.status_code != 200:
            bot.send_message(chat_id, f'❌ SD WebUI вернул HTTP {r.status_code}')
            return

        resp     = r.json()
        img_bytes = base64.b64decode(resp['images'][0])

        # Extract actual seed used
        try:
            info = json.loads(resp.get('info', '{}'))
            seed = info.get('seed', -1)
        except Exception:
            seed = -1

        elapsed = int(time.time() - start_ts)

        # Image
        img = Image.open(BytesIO(img_bytes)).convert('RGB')
        img_buf = BytesIO()
        img.save(img_buf, 'JPEG', quality=92)
        img_buf.seek(0)

        bot.send_photo(
            chat_id, img_buf,
            caption=f'✅ Готово за {elapsed // 60}м {elapsed % 60:02d}с\n'
                    f'*{STYLE_RU[style]}* · *{ROOM_RU[room]}*',
            parse_mode='Markdown',
        )

        # Report document
        report_text = build_report(style, room, setup, selections, prompt, seed, elapsed)
        report_buf  = BytesIO(report_text.encode('utf-8'))
        report_buf.name = f'report_{style}_{room}.txt'
        bot.send_document(chat_id, report_buf, caption='📄 Параметры генерации')

    except requests.Timeout:
        stop_event.set()
        bot.send_message(chat_id, '❌ SD WebUI не ответил за 10 минут — попробуй ещё раз.')
    except Exception as e:
        stop_event.set()
        bot.send_message(chat_id, f'❌ Ошибка: {str(e)[:200]}')


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
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(telebot.types.KeyboardButton(
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
