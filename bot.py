"""
Interior Constructor Bot
Receives web_app_data from Mini App, generates a room render via SD WebUI,
sends status updates every 30s, delivers image + full report to user.
"""
import json, re, threading, time, base64, requests, telebot
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
ROOM_RU  = {'living': 'Гостиная', 'bedroom': 'Спальня', 'bathroom': 'Ванная', 'kitchen': 'Кухня'}
CATS     = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm')]
CAT_RU   = {'furniture': 'Мебель', 'lighting': 'Освещение', 'materials': 'Материалы'}

# Russian slot labels per room (matches ROOM_LABELS in index.html)
SLOT_LABELS = {
    'living': {
        'furniture': ['Диван','Журнальный стол','Кресло','Полка / ТВ-тумба','Ширма','Шкаф / Комод','Пуф / Табурет','Напольные подушки','Стеллаж'],
        'lighting':  ['Подвес','Торшер','Скрытая LED','Настольная лампа','Бра','Абажур','Потолочные споты','Декор. светильник','Акцентный свет'],
        'materials': ['Стены (цвет)','Пол','Стены (панели)','Текстиль','Камень','Акцентные детали','Керамика','Ковёр','Шторы'],
    },
    'bedroom': {
        'furniture': ['Кровать','Прикроватные тумбы','Шкаф','Банкетка','Туалетный столик','Акцентное кресло','Зеркало','Вешалка','Хранение'],
        'lighting':  ['Подвес над кроватью','Прикроватные лампы','Бра','LED под кроватью','Торшер','Потолочные споты','Подсветка шкафа','Навигационный свет','Основной свет'],
        'materials': ['Стены','Пол','Изголовье','Постельное бельё','Ковёр','Шторы','Декор','Фурнитура','Металлические акценты'],
    },
    'bathroom': {
        'furniture': ['Тумба под раковину','Скамья','Пенал','Корзина','Раковина','Полка на ванну','Зеркало','Скамья в душевой','Хранение'],
        'lighting':  ['Подсветка зеркала','Подвес','LED-лента','Потолочные','LED под тумбой','Основной свет','Бра','Закарнизный свет','Над ванной'],
        'materials': ['Стены','Потолок','Пол','Пол в душевой','Сантехника','Текстиль','Акценты','Декор','Перегородки'],
    },
    'kitchen': {
        'furniture': ['Кухонный остров','Барный стул','Обеденный стол','Навесная полка','Шкаф-пенал','Тележка','Мойка','Смеситель','Разделочная доска'],
        'lighting':  ['Подвес над островом','Бра рабочей зоны','Встроенная подсветка','Потолочный спот','Трековый светильник','Лампа на подоконник','Безрамный светильник','Умный выключатель','Подсветка цоколя'],
        'materials': ['Фартук','Столешница','Фасад кухни','Текстиль','Посуда','Декор','Фурнитура','Пол','Окно'],
    },
}

# Russian variant names per slot (matches ROOM_OPTS in index.html)
SLOT_OPTS = {
    'living': {
        'furniture': [['Модульный','Футон'],['Бионический','Травертин'],['Ротанг','Букле'],['ТВ-тумба','Парящая полка'],['Реечная','Сёдзи'],['Встроенный','Отдельный'],['Бамбук','Деревянный пень'],['Подушки','Скамья'],['Открытый','Закрытый']],
        'lighting':  [['Akari','Матовая сфера'],['Бумажный','Льняной'],['LED-лента','Безрамочные'],['Керамика','Бамбук'],['Направленный','Рассеянный'],['Ротанг','Плиссе'],['Накладные','Магнитный трек'],['Переносной','Каменный'],['За экранами','Подоконник']],
        'materials': [['Беж','Шалфей'],['Дуб','Микроцемент'],['Рейки','Штукатурка'],['Лён','Букле'],['Травертин','Керамогранит'],['Бамбук','Тёмный орех'],['Ваби-саби','Гладкая'],['Джут','Килим'],['Рисовая бумага','Жалюзи']],
    },
    'bedroom': {
        'furniture': [['Подиум','Мягкое изголовье'],['Консольные','Керамический'],['Сёдзи-слайдеры','Открытое'],['Банкетка','Сундук'],['Столик','Парящая полка'],['Ротанг','Букле'],['Напольное','Настенное'],['Камердинер','Рейка с крюками'],['Встроенное','Комод']],
        'lighting':  [['Фонарь','Матовое стекло'],['Бамбуковые','Керамика'],['Чтение','Латунные'],['Под кроватью','За изголовьем'],['Зона чтения','Направленное бра'],['Безрамочные','Накладные'],['Внутри шкафа','На фасады'],['Плинтус','Встроенные'],['Диммируемый','Умная система']],
        'materials': [['Штукатурка','Матовая краска'],['Дуб/ясень','Татами'],['Деревянное','Лён'],['Стираный лён','Органик хлопок'],['Джут','Шерстяной'],['Экраны сёдзи','Льняные'],['Глина','Дрейфвуд'],['Каменная','Скрытая'],['Чёрный матовый','Бронза']],
    },
    'bathroom': {
        'furniture': [['Подвесная','Бетонная'],['Деревянная','Бамбуковая'],['Деревянный','В нишах'],['Деревянная','Плетёная'],['Каменная','Керамическая'],['Деревянная','Металлическая'],['Без рамы','В раме-полке'],['Встроенная','Тик'],['За зеркалом','Парящие полки']],
        'lighting':  [['Backlight','Бра по бокам'],['Матовое стекло','Деревянные'],['LED-ниша','Спот в душе'],['Безрамочные','Плоские'],['Под тумбой','Напольный'],['2700K','Переменная'],['Керамика','Латунь'],['Закарнизный','Skylight'],['Подвесной','Настенный']],
        'materials': [['Tadelakt','Керамогранит'],['Реечный','Окрашенный'],['Микроцемент','Под дерево'],['Галька','Рейки'],['Чёрная','Gunmetal'],['Вафельные','Льняные'],['Травертин','Сланец'],['Бамбуковый','Диатомит'],['Матовое','Прозрачное']],
    },
    'kitchen': {
        'furniture': [['Камень и рейки','Тёмный дуб'],['Ротанг','Металл'],['Массив','Белый'],['Открытая','Со стеклом'],['Сёдзи','Матовый белый'],['Деревянная','Металлическая'],['Керамика','Нержавейка'],['Двойной','Одинарный'],['Дерево (спил)','Мрамор']],
        'lighting':  [['Рисовая бумага','Индастриал'],['Бра','LED-лента'],['Ниша','Споты'],['Споты','Накладные'],['Трек','Магнитный'],['Настенный','Гусиная шея'],['Встроенный','Панель'],['Деревянная панель','Сенсорная'],['Цоколь','Плинтус']],
        'materials': [['Штукатурка','Zellige'],['Травертин','Марокканский мрамор'],['Шпон','Шалфей'],['Лён','Вафельный'],['Керамика','Белая керамика'],['Бамбук','Терракота'],['Скрытая','Латунная'],['Микроцемент','Бетон'],['Сёдзи','Лён']],
    },
}

NEG = ('people, person, human figure, ugly, deformed, noisy, blurry, low resolution, '
       'oversaturated, flat lighting, text, watermark, logo, clutter, dark')

def main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton(
        text='🏠 Открыть конструктор',
        web_app=telebot.types.WebAppInfo(url='https://tp9mc.github.io'),
    ))
    return markup


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
    """English-only keyword phrase from positive prompt for scene prompt."""
    pos = item.get('positive') or ''
    if not pos:
        return ''
    # Strip prefix labels
    pos = re.sub(r'^[Pp]ositive\s*[Pp]rompt\s*\)?\s*:?\s*', '', pos)
    pos = re.sub(r'^[Pp]ositive\s*:?\s*', '', pos)
    clean = strip_boilerplate(pos)
    # Filter per-term: drop any segment containing Cyrillic
    segs = [s.strip() for s in clean.split(',')
            if s.strip() and not re.search(r'[а-яёА-ЯЁ]', s)]
    return ', '.join(segs[:3])[:120]


def build_prompt_and_report(style, room, setup, catalog):
    """
    Returns (prompt_str, neg_str, selections_list).
    selections_list: list of (cat_id, n, variant, name_ru, phrase) for all 27 slots.
    """
    scene_parts = [STYLE_DNA[style], ROOM_DNA[room]]
    neg_parts   = [NEG]
    selections  = []  # (cat_id, n, variant, name_ru, phrase)

    for cat_id, px in CATS:
        cat_setup = setup.get(cat_id, {})
        for n in range(1, 10):
            slot_key = f'{px}_{n}'
            variant  = cat_setup.get(slot_key, 'main')
            item     = get_item(catalog, style, room, cat_id, n, variant)
            name_ru  = item.get('name_ru', '')
            phrase   = item_phrase(item)
            selections.append((cat_id, n, variant, name_ru, phrase))
            if phrase:
                scene_parts.append(phrase)
            # Collect item-level negative prompt — strip prefix label, filter per-term
            item_neg = item.get('negative') or ''
            if item_neg:
                item_neg = re.sub(r'^[Nn]egative\s*[Pp]rompt\s*\)?\s*:?\s*', '', item_neg)
                item_neg = re.sub(r'^[Nn]egative\s*:?\s*', '', item_neg)
                clean_terms = [t.strip() for t in item_neg.split(',')
                               if t.strip() and not re.search(r'[а-яёА-ЯЁ]', t)]
                if clean_terms:
                    neg_parts.append(', '.join(clean_terms))

    scene_parts += [
        'professional interior photography', 'natural daylight',
        'wide angle lens', 'high quality', 'architectural digest',
        'photo realistic', '8k',
    ]
    prompt = ', '.join(p for p in scene_parts if p)
    # Deduplicate neg terms
    seen, neg_dedup = set(), []
    for part in neg_parts:
        for term in part.split(','):
            t = term.strip().lower()
            if t and t not in seen:
                seen.add(t)
                neg_dedup.append(term.strip())
    neg = ', '.join(neg_dedup)
    return prompt, neg, selections


def build_report(style, room, setup, selections, prompt, neg, seed, elapsed_sec):
    W = 62
    lines = []

    # ── Шапка ──────────────────────────────────────────────────
    lines += ['═' * W, '  КОНФИГУРАЦИЯ ИНТЕРЬЕРА', '═' * W, '']
    lines.append(f'  Дата:    {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  Стиль:   {STYLE_RU[style]}')
    lines.append(f'  Комната: {ROOM_RU[room]}')
    lines.append('')

    # ── Выборы пользователя (3 категории × 9 слотов) ──────────
    for cat_id, px in CATS:
        lines += ['─' * W, f'  {CAT_RU[cat_id].upper()}', '─' * W]
        slot_labels = SLOT_LABELS.get(room, {}).get(cat_id, [])
        slot_opts   = SLOT_OPTS.get(room, {}).get(cat_id, [])
        cat_sels = [(n, v, nr, ph) for (c, n, v, nr, ph) in selections if c == cat_id]
        for n, variant, name_ru, phrase in cat_sels:
            label    = slot_labels[n - 1] if n - 1 < len(slot_labels) else f'{px}_{n}'
            vi       = 0 if variant == 'main' else 1
            var_name = slot_opts[n - 1][vi] if n - 1 < len(slot_opts) else ('A' if vi == 0 else 'Б')
            # Use name_ru from catalog if available, otherwise var_name
            display  = name_ru if name_ru else var_name
            lines.append(f'  {n:2}. {label:<26} [{var_name}]  {display}')
        lines.append('')

    # ── Технические параметры ──────────────────────────────────
    lines += ['═' * W, '  ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ', '═' * W, '']
    lines.append(f'  Модель:    Juggernaut XL v9')
    lines.append(f'  Сэмплер:   Euler / Simple')
    lines.append(f'  Шаги:      30')
    lines.append(f'  CFG:       7')
    lines.append(f'  Seed:      {seed}')
    lines.append(f'  Размер:    1024×576')
    lines.append(f'  Время:     {elapsed_sec // 60}м {elapsed_sec % 60:02d}с')
    lines.append('')
    lines += ['─' * W, '  ПРОМТ (EN)', '─' * W]
    lines.append(prompt)
    lines.append('')
    lines += ['─' * W, '  НЕГАТИВНЫЙ ПРОМТ', '─' * W]
    lines.append(neg)
    lines += ['', '═' * W]
    return '\n'.join(lines)


def generate_room(chat_id: int, payload: dict, bot: telebot.TeleBot):
    style = payload.get('style', '')
    room  = payload.get('room', '')
    setup = payload.get('setup', {})

    if style not in STYLE_DNA or room not in ROOM_DNA:
        bot.send_message(chat_id, '❌ Неизвестный стиль или комната.')
        return

    catalog    = load_catalog()
    prompt, neg, selections = build_prompt_and_report(style, room, setup, catalog)

    bot.send_message(
        chat_id,
        f'🏠 Генерирую *{STYLE_RU[style]}* · *{ROOM_RU[room]}*\n'
        f'Это займёт 1–3 минуты, буду присылать обновления.',
        parse_mode='Markdown',
        reply_markup=main_keyboard(),
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
            'negative_prompt':   neg,
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
        report_text = build_report(style, room, setup, selections, prompt, neg, seed, elapsed)
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

    @bot.message_handler(commands=['start', 'restart'])
    def on_start(message):
        bot.send_message(
            message.chat.id,
            '👋 Привет! Выбери стиль и комнату, нажми «Сгенерировать» — '
            'я создам рендер и пришлю сюда.',
            reply_markup=main_keyboard(),
        )

    print('Bot started, polling…')
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == '__main__':
    main()
