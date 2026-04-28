"""
Interior Constructor Bot
Receives web_app_data from Mini App, generates a room render via SD WebUI,
sends status updates every 30s, delivers image + full report to user.
"""
import json, re, threading, time, base64, requests, telebot, subprocess, signal, os
from io import BytesIO
from PIL import Image
from datetime import datetime
from collections import defaultdict

from bot_secrets import BOT_TOKEN  # not committed to git
SD_URL        = 'http://localhost:7860/sdapi/v1/txt2img'
SD_HEALTH     = 'http://localhost:7860/sdapi/v1/sd-models'
SD_WEBUI_DIR  = '/Users/timofeev_sd/stable-diffusion-webui'
CATALOG       = '/tmp/catalog.json'
SD_TIMEOUT    = 600
STATS_LOG     = '/tmp/bot_stats.jsonl'
IDLE_TIMEOUT  = 15 * 60  # seconds before SD is shut down

_last_gen_time = 0.0
_sd_lock       = threading.Lock()

_log_lock = threading.Lock()


def sd_is_up() -> bool:
    try:
        return requests.get(SD_HEALTH, timeout=3).status_code == 200
    except Exception:
        return False


def sd_stop():
    try:
        result = subprocess.run(['lsof', '-ti', ':7860'], capture_output=True, text=True)
        for pid in result.stdout.strip().split():
            os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


def sd_ensure_up() -> bool:
    """Start SD WebUI if not running; wait up to 120s. Returns True when ready."""
    with _sd_lock:
        if sd_is_up():
            return True
        subprocess.Popen(
            ['bash', 'webui.sh', '--api', '--nowebui', '--port', '7860'],
            cwd=SD_WEBUI_DIR,
            stdout=open('/tmp/sdwebui.log', 'w'),
            stderr=subprocess.STDOUT,
        )
        for _ in range(120):
            time.sleep(1)
            if sd_is_up():
                return True
        return False


def _idle_watchdog():
    global _last_gen_time
    while True:
        time.sleep(60)
        if _last_gen_time and time.time() - _last_gen_time > IDLE_TIMEOUT and sd_is_up():
            sd_stop()
            _last_gen_time = 0.0


threading.Thread(target=_idle_watchdog, daemon=True).start()

def log_event(user_id: int, username: str, action: str, details: dict = None):
    entry = {
        'ts':       datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'user_id':  user_id,
        'username': username or str(user_id),
        'action':   action,
    }
    if details:
        entry.update(details)
    with _log_lock:
        with open(STATS_LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, ensure_ascii=False) + '\n')


def build_stats_report() -> str:
    try:
        with open(STATS_LOG, encoding='utf-8') as f:
            events = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return 'Событий пока нет.'

    users        = {}   # user_id → {username, actions}
    style_count  = defaultdict(int)
    room_count   = defaultdict(int)
    gen_times    = []
    gen_ok       = 0
    gen_fail     = 0

    for e in events:
        uid  = e['user_id']
        uname = e.get('username', str(uid))
        if uid not in users:
            users[uid] = {'username': uname, 'actions': defaultdict(int), 'first': e['ts']}
        users[uid]['actions'][e['action']] += 1
        users[uid]['last'] = e['ts']

        if e['action'] == 'gen_ok':
            gen_ok += 1
            style_count[e.get('style', '?')] += 1
            room_count[e.get('room', '?')]   += 1
            if 'elapsed' in e:
                gen_times.append(e['elapsed'])
        elif e['action'] == 'gen_fail':
            gen_fail += 1

    W = 62
    lines = []
    lines += ['═' * W, '  СТАТИСТИКА БОТА', '═' * W, '']
    lines.append(f'  Дата отчёта:   {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  Всего событий: {len(events)}')
    lines.append(f'  Уникальных пользователей: {len(users)}')
    lines.append(f'  Генераций успешных: {gen_ok}')
    lines.append(f'  Генераций с ошибкой: {gen_fail}')
    if gen_times:
        avg = sum(gen_times) / len(gen_times)
        lines.append(f'  Среднее время генерации: {int(avg)//60}м {int(avg)%60:02d}с')
    lines.append('')

    if style_count:
        lines += ['─' * W, '  СТИЛИ (по числу генераций)', '─' * W]
        for s, n in sorted(style_count.items(), key=lambda x: -x[1]):
            lines.append(f'  {STYLE_RU.get(s, s):<22} {n}')
        lines.append('')

    if room_count:
        lines += ['─' * W, '  КОМНАТЫ (по числу генераций)', '─' * W]
        for r, n in sorted(room_count.items(), key=lambda x: -x[1]):
            lines.append(f'  {ROOM_RU.get(r, r):<22} {n}')
        lines.append('')

    lines += ['─' * W, '  ПОЛЬЗОВАТЕЛИ', '─' * W]
    for uid, u in sorted(users.items(), key=lambda x: -sum(x[1]['actions'].values())):
        acts = u['actions']
        total_acts = sum(acts.values())
        gens  = acts.get('gen_ok', 0)
        lines.append(f'  @{u["username"]} (id {uid})')
        lines.append(f'    Всего действий: {total_acts}  |  Генераций: {gens}')
        lines.append(f'    Первое: {u["first"]}  |  Последнее: {u.get("last","")}')
        details = ', '.join(f'{k}={v}' for k, v in sorted(acts.items()))
        lines.append(f'    Действия: {details}')
        lines.append('')

    lines += ['─' * W, '  ПОЛНЫЙ ЛОГ СОБЫТИЙ', '─' * W]
    for e in events:
        detail = {k: v for k, v in e.items() if k not in ('ts', 'user_id', 'username', 'action')}
        detail_str = ('  ' + ', '.join(f'{k}={v}' for k, v in detail.items())) if detail else ''
        lines.append(f'  {e["ts"]}  @{e["username"]}  {e["action"]}{detail_str}')

    lines += ['', '═' * W]
    return '\n'.join(lines)

STYLE_DNA = {
    'japandi':        'japandi interior design, warm oak wood, natural linen fabric, wabi-sabi minimalism, earth tones, zen atmosphere',
    'modern_classic': 'modern classic interior design, Carrara marble surfaces, deep velvet upholstery, polished brass accents, grand symmetry',
    'scandi':         'scandinavian interior design, light birch wood, crisp white walls, soft wool textiles, hygge coziness, natural daylight',
}
ROOM_DNA = {
    'living':   'living room interior, large windows, natural light, clean empty floor',
    'bedroom':  'bedroom interior, natural light, clean empty floor',
    'bathroom': 'bathroom interior, natural light, clean surfaces',
    'kitchen':  'kitchen interior, natural light, clean surfaces',
}
STYLE_RU = {'japandi': 'Japandi', 'modern_classic': 'Модерн Классик', 'scandi': 'Скандинавский'}
ROOM_RU  = {'living': 'Гостиная', 'bedroom': 'Спальня', 'bathroom': 'Ванная', 'kitchen': 'Кухня'}
CATS     = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm')]
CAT_RU   = {'furniture': 'Мебель', 'lighting': 'Освещение', 'materials': 'Материалы'}

# Russian slot labels per room (matches ROOM_LABELS in index.html)
SLOT_LABELS = {
    'japandi': {
        'living': {
            'furniture': ['Низкий модульный диван','Журнальный стол','Кресло с плетением','Низкая консоль под ТВ','Деревянная реечная','Встроенные шкафы','Табурет из бамбука','Напольные подушки','Отдельно стоящий'],
            'lighting': ['Светильник из рисовой','Низкий торшер','Скрытая LED-подсветка','Настольная лампа','Настенное бра','Плетеный абажур','Потолочные споты','Переносной','Подсветка'],
            'materials': ['Стены: Теплые бежевые','Пол: Светлый дуб','Стены: Реечные','Текстиль: Натуральный','Камень : Природный','Детали: Бамбук','Керамика: Ваби-саби','Ковер: Джут или сизаль','Шторы: Рулонные'],
        },
        'bedroom': {
            'furniture': ['Низкая кровать-подиум','Подвесные консольные','Шкаф','Деревянная банкетка','Низкий туалетный','Акцентное кресло','Напольное зеркало','Прикроватная вешалка','Встроенные незаметные'],
            'lighting': ['Бумажный','Бамбуковые','Бра для чтения','LED-подсветка','Торшер для зоны чтения','Безрамочные','Внутренняя','Навигационная','Диммируемый теплый'],
            'materials': ['Стены : Известковая','Пол: Деревянный','Мебель : Деревянное','Текстиль: Постельное','Ковер: Джут возле','Шторы: Оконные экраны','Декор: Глиняный','Фурнитура: Каменная','Детали: Черные'],
        },
        'bathroom': {
            'furniture': ['Подвесная тумба','Деревянная скамья','Высокий узкий','Минималистичная','Раковина из цельного','Деревянная','Зеркало без рамы','Встроенная деревянная','Скрытое хранение'],
            'lighting': ['Фоновая подсветка','Подвес над раковиной','Влагозащищенная','Встроенные потолочные','LED-подсветка','Теплый диммируемый','Керамическое','Отраженный','Подвесной светильник'],
            'materials': ['Стены: Влагостойкая','Потолок: Светлый','Пол : Микроцемент','Душевая зона: Галька','Сантехника : Матовая','Текстиль: Вафельные','Акценты : Травертин','Декор : Бамбуковый','Перегородки : Матовое'],
        },
        'kitchen': {
            'furniture': ['Кухонный остров','Барный стул','Низкий обеденный стол','Открытая навесная','Высокий шкаф-пенал','Деревянная тележка','Керамическая мойка','Кухонный смеситель','Разделочная доска'],
            'lighting': ['Подвес над островом','Бра рабочей зоны','LED-профиль','Потолочный спот','Трековый светильник','Настольная лампа','Встроенный','Умный выключатель','Подсветка цоколя'],
            'materials': ['Фартук : Рельефная','Столешница : Травертин','Фасад кухни : Шпон','Текстиль : Грубое','Посуда : Керамическая','Декор : Бамбуковая','Фурнитура : Скрытая','Пол : Микроцемент','Окно : Рулонная штора'],
        },
    },
    'modern_classic': {
        'living': {
            'furniture': ['Диван честерфилд','Журнальный стол','Кресла с каретной','Встроенные книжные','Консольный стол','Пуф-банкетка','Классический каминный','Наборные столики','Мягкие банкетки'],
            'lighting': ['Хрустальная люстра','Симметричные','Подсветка картин','Высокие лампы-буфеты','Торшер','Многоярусные','Закарнизная подсветка','Лампа для чтения','Центральное'],
            'materials': ['Стены: Гипсовые','Пол: Паркет','Камень: Светлый мрамор','Текстиль: Бархат','Детали: Античная','База: Слоновая кость','Декор: Зеркала','Текстиль: Тяжелые','Детали: Высокие'],
        },
        'bedroom': {
            'furniture': ['Кровать','Прикроватные тумбы','Шкаф','Банкетка в изножье','Туалетный столик','Мягкое акцентное','Напольное зеркало','Комод','Круглые прикроватные'],
            'lighting': ['Люстра','Стеклянные/хрустальные','Настенные бра','Подсветка картин','Торшер для зоны чтения','Внутренняя подсветка','Бра по бокам','Мягкая нижняя','Фигурная потолочная'],
            'materials': ['Обои с дамаском','Ковровое покрытие','Текстиль: Шелк','Декор : Бархатные','Детали: Хрустальные','Декор : Искусственный','Текстиль : Шторы','Стены: Деревянные','Акценты: Пудровый'],
        },
        'bathroom': {
            'furniture': ['Отдельно стоящая ванна','Тумба под раковину','Высокий шкаф-пенал','Мягкий пуф','Керамические раковины','Зеркало','Душевая кабина','Вешалка для полотенец','Декоративная полка'],
            'lighting': ['Небольшая люстра','Парные бра по бокам','Потолочный плафон','LED-подсветка','Косметическое зеркало','Подсветка под тумбой','Подсветка','Подвесы','Диммируемый свет'],
            'materials': ['Плитка кабанчик','Мозаика на полу','Фурнитура никель','Деревянные панели','Смесители','Рифленое стекло','Напольный смеситель','Махровые полотенца','Влагостойкая'],
        },
        'kitchen': {
            'furniture': ['Остров','Барный полукресло','Обеденный стол','Витрина для посуды','Портал для вытяжки','Напольный шкаф','Сервировочная тележка','Смеситель','Винный шкаф'],
            'lighting': ['Люстра над столом','Бра на фартуке','Линейный подвес','Внутренняя подсветка','Спот для подсветки','Потолочный плафон','Лампа - буфет','Светильник','Ретро - выключатель'],
            'materials': ['Фартук : Плитка','Столешница : Темный','Фасад : Филенка','Французская елка','Фурнитура : Латунная','Текстиль : Хлопковое','Посуда : Фарфоровая','Декор : Мраморная','Окно : Римская штора'],
        },
    },
    'scandi': {
        'living': {
            'furniture': ['Модульный диван','Наборные столики','Кресло','ТВ-консоль','Открытые модульные','Пуф','Настенный стол','Обеденный стол','Обеденные стулья'],
            'lighting': ['Металлический','Шарнирный','Магнитные трековые','Металлическая','Бра на поворотном','Открытая лампа','LED-профили','Переносной','Архитектурный'],
            'materials': ['Стены: Холодные','Пол: Выбеленный дуб','Текстиль : Шерсть','Детали: Кожа','Светлая березовая','Фурнитура : Матовая','Декор: Накидки','Поверхности Soft-touch','Графичные черные'],
        },
        'bedroom': {
            'furniture': ['Каркас кровати','Парящие прикроватные','Шкафы','Минималистичная','Туалетный столик','Кресло для чтения','Зеркало, прислоненное','Настенная штанга','Низкий комод'],
            'lighting': ['Подвес из сложенной','Регулируемые','Стеклянная лампа-гриб','Трековое освещение','Утопленные потолочные','Торшер для чтения','LED-профиль','Навигационный свет','Рассеянный оконный'],
            'materials': ['Глубокоматовая краска','Пол: Светлая сосна','Текстиль: Постельное','Текстиль : Шерстяные','Детали: Кожаные','Льняные рулонные шторы','Детали: Видимый срез','База: Приглушенная','Декор: Тактильная'],
        },
        'bathroom': {
            'furniture': ['Парящая тумба','Отдельно стоящая','Навесной','Табурет из массива','Раковина','Круглое зеркало','Минималистичная','Унитаз со скрытым','Комод'],
            'lighting': ['Мягкая аура-подсветка','Встроенные потолочные','Подвес','LED-подсветка','Сенсорная ночная','Яркий дневной свет','Косметическое зеркало','Свет из зенитного окна','Подвесной светильник'],
            'materials': ['Плитка кабанчик 3D r','Керамогранит','Сантехника: Матовая','Реечный коврик','Стекло душевой','Поверхности: Corian','Потолок : Гладкий','Монохромная','Вафельные льняные'],
        },
        'kitchen': {
            'furniture': ['Гладкий кухонный','Деревянный барный','Раздвижной обеденный','Обеденный стул','Модульный стеллаж','Табурет - стремянка','Смеситель с гибким','Мусорное ведро','Перфорированная панель'],
            'lighting': ['Многоярусный подвес','Бра на шарнирном','Открытая лампа','Гладкий потолочный','Магнитный трек','Диммер','Лампа-грибок','Светодиодная лента','Настенный светильник'],
            'materials': ['Фартук : Бесшовное','Столешница : Матовый','Фасад : Светлая фанера','Пол : Светлый','Фурнитура : Матовая','Текстиль : Шерстяная','Посуда : Матовая','Декор : Пробковая','Окно : Гладкие'],
        },
    },
}

SLOT_OPTS = {
    'japandi': {
        'living': {
            'furniture': [['Низкий','Футон'],['Журнальный','Низкий'],['С плетением','Лаунж-кресло'],['Низкая консоль','Парящая полка'],['Деревянная','Полупрозрачная'],['Встроенные','Отдельно'],['Из бамбука','Из цельного'],['Напольные','Низкие'],['Отдельно','Закрытая']],
            'lighting': [['Из рисовой','Сфера'],['Низкий торшер','Торшер'],['Скрытая','Встроенные'],['Настольная','Настольная'],['Настенное бра','Керамические'],['Плетеный абажур','Крупно'],['Потолочные','Тонкие'],['Переносной','Маленькая лампа'],['Подсветка','Теплая']],
            'materials': [['Теплые бежевые','Приглушенные'],['Светлый дуб','Микроцемент'],['Реечные','Гладкая'],['Натуральный','Фактурное букле'],['Природный','Матовый'],['Детали: Бамбук','Детали'],['Ваби-саби','Гладкая'],['Ковер: Джут','Ковер'],['Шторы','Шторы']],
        },
        'bedroom': {
            'furniture': [['Низкая','Без видимых'],['Подвесные','Керамический'],['Шкаф','Открытая'],['Деревянная','Минималистичный'],['Низкий','Парящая полка'],['Акцентное','Место'],['Напольное','Настенное'],['Прикроватная','Деревянная'],['Встроенные','Низкий']],
            'lighting': [['Бумажный','Подвес'],['Бамбуковые','Лампы'],['Для чтения','Шарнирные'],['LED-подсветка','LED-подсветка'],['Для зоны чтения','Настенное бра'],['Безрамочные','Накладные'],['Внутренняя','Направленные'],['Навигационная','Низкие'],['Диммируемый','Умная система']],
            'materials': [['Известковая','Глубокоматовая'],['Деревянный','Плетеное'],['Мебель','Мебель'],['Постельное','Постельное'],['Ковер: Джут','Ковер : Мягкий'],['Шторы: Оконные','Шторы'],['Глиняный','Выбеленная'],['Каменная','Скрытая'],['Детали: Черные','Детали']],
        },
        'bathroom': {
            'furniture': [['Подвесная','Монолитная'],['Деревянная','Бамбуковая'],['Высокий узкий','Встроенные'],['Минималистичная','Плетеная'],['Из цельного','Матовая'],['Деревянная','Минималистичная'],['Без рамы','В глубокой'],['Встроенная','Отдельно'],['Скрытое','Открытые']],
            'lighting': [['Фоновая','Пара'],['Над раковиной','Цилиндрические'],['Влагозащищенная','Направленный'],['Встроенные','Плоские плафоны'],['LED-подсветка','Навигационная'],['Теплый','Свет'],['Керамическое','Влагостойкое'],['Отраженный','Имитация'],['Подвесной','Настенный']],
            'materials': [['Влагостойкая','Крупноформатный'],['Светлый','Гладкий'],['Микроцемент','Матовый'],['Душевая зона','Душевая зона'],['Матовая черная','Вороненая сталь'],['Вафельные','Льняные банные'],['Травертин','Сланец'],['Бамбуковый','Коврик'],['Перегородки','Перегородки']],
        },
        'kitchen': {
            'furniture': [['Камень и рейки','Тёмный дуб'],['Ротанг и дерево','Металл'],['Массив','Белый лак'],['Парящая','Матовое стекло'],['Сёдзи','Белый матовый'],['Тележка','Нержавейка'],['Мойка','Нержавейка'],['Минимализм','Никель'],['Цельный спил','Мрамор']],
            'lighting': [['Рисовая бумага','Индустриал'],['Керамика','LED-лента'],['Профиль','Точечные'],['Дерево','Накладные'],['Трековый','Магнитный'],['Фонарь','Гусиная шея'],['Безрамочный','Панель'],['Деревянная','Сенсорный'],['Цоколь','Плинтус']],
            'materials': [['Штукатурка','Zellige'],['Травертин','Мрамор'],['Шпон','Шалфей'],['Льняное','Хлопок'],['Пиала','Белая керамика'],['Бамбук','Терракота'],['Дерево','Латунь'],['Микроцемент','Бетон'],['Сёдзи','Лён']],
        },
    },
    'modern_classic': {
        'living': {
            'furniture': [['Честерфилд','Лаконичный'],['Журнальный стол','Стеклянный'],['Кресла','Поворотные'],['Встроенные','Симметричные'],['Консольный стол','Комоды'],['Банкетка','Пара'],['Классический','Современный'],['Наборные','Приставные'],['Мягкие','Металлические']],
            'lighting': [['Хрустальная','Латунный'],['Симметричные','В виде'],['Подсветка','Встроенные'],['Высокие','Настольные'],['Торшер','Тренога'],['Многоярусные','Геометричные'],['Закарнизная','Плоские'],['Для чтения','В аптекарском'],['Центральное','Зонированный']],
            'materials': [['Гипсовые','Плотные обои'],['Паркет','Широкоформатная'],['Светлый мрамор','Темный мрамор'],['Бархат','Плотный лен'],['Детали','Детали'],['База: Слоновая','Акцент'],['Зеркала','Зеркала'],['Тяжелые','Римские шторы'],['Детали','Детали']],
        },
        'bedroom': {
            'furniture': [['Кровать','Кровать'],['Прикроватные','Тумбы'],['Шкаф','Гардероб'],['В изножье','Пара'],['Туалетный','Классическое'],['Мягкое','Кушетка'],['Напольное','Крупное зеркало'],['Комод','Высокий узкий'],['Круглые','Прикроватный']],
            'lighting': [['Люстра','Прилегающий'],['Стеклянные/хрус','Металлические'],['Настенные бра','Шарнирные'],['Подсветка','Встроенные'],['Для зоны чтения','В аптекарском'],['Внутренняя','Штанги со'],['По бокам','Лампы'],['Мягкая нижняя','Подсветка'],['Фигурная','Гладкий']],
            'materials': [['Обои с дамаском','Окрашенные'],['Ковровое','Классический'],['Шелк или сатин','Египетский'],['Бархатные','Подушки'],['Детали','Детали: Ручки'],['Искусственный','Кашемировые'],['Шторы блэкаут','Шелковые'],['Деревянные','Гладкая'],['Пудровый','Монохромная']],
        },
        'bathroom': {
            'furniture': [['Отдельно','Встроенная'],['Под раковину','Подвесная'],['Высокий','Открытые полки'],['Мягкий пуф','Табурет'],['Керамические','Накладные'],['Зеркало','Зеркало'],['Душевая кабина','Полностью'],['Отдельно','Настенные'],['Декоративная','Встроенная ниша']],
            'lighting': [['Небольшая','Влагозащищенный'],['Парные бра','Линейный'],['Потолочный','Равномерное'],['LED-подсветка','Направленный'],['Косметическое','Настенный'],['Подсветка','Навигационная'],['Подсветка','Декоративный'],['Подвесы','Подвесы'],['Диммируемый','Яркий дневной']],
            'materials': [['Плитка кабанчик','Крупноформатные'],['Мозаика на полу','Два вида'],['Полированный','Нелакированная'],['Деревянные','Плитка'],['Смесители','Классические'],['Рифленое','Абсолютно'],['Напольный','Современный'],['Махровые','Турецкие'],['Влагостойкая','Влагостойкие']],
        },
        'kitchen': {
            'furniture': [['Мрамор','Тёмный дуб'],['Бархат','Металл'],['Пьедестал','Белый лак'],['Стекло','Матовое стекло'],['Лепнина','Белый матовый'],['Ящики','Нержавейка'],['Латунь','Нержавейка'],['Крестовые','Никель'],['Встраиваемый','Мрамор']],
            'lighting': [['Хрусталь','Индустриал'],['Колбы','LED-лента'],['Линейный','Точечные'],['Подсветка','Накладные'],['Спот','Магнитный'],['Плафон','Гусиная шея'],['Буфет','Панель'],['Направленный','Сенсорный'],['Тумблер','Плинтус']],
            'materials': [['Кабанчик','Zellige'],['Темный мрамор','Мрамор'],['Шейкер','Шалфей'],['Ёлочка','Хлопок'],['Ракушка','Белая керамика'],['Вышивка','Терракота'],['Фарфор','Латунь'],['Ступка','Бетон'],['Римская','Лён']],
        },
    },
    'scandi': {
        'living': {
            'furniture': [['Эргономичный','Гладкий диван'],['Наборные','Столики'],['Кресло','Скульптурное'],['Парящая','Комод'],['Открытые','Встроенные'],['Мягкий','Деревянный'],['Парящий','Лаконичная'],['Из массива','Стол'],['Стулья Y-спинка','Стулья']],
            'lighting': [['Металлический','Подвес'],['Шарнирный','Удочка'],['Магнитные','Встроенные'],['Металлическая','Настольная'],['На поворотном','Линейный'],['Открытая лампа','Керамический'],['LED-профили','Направленные'],['Переносной','Напольный'],['Архитектурный','Диммируемый']],
            'materials': [['Холодные','Пастельные тона'],['Выбеленный','Гладкий'],['Шерсть , войлок','Хлопок и лен'],['Детали: Кожа','Детали'],['Светлая','Массив дуба'],['Матовая черная','Брашированная'],['Накидки','Пледы'],['Поверхности','Натуральное'],['Графичные','Детали в тон']],
        },
        'bedroom': {
            'furniture': [['Каркас кровати','Кровать'],['Парящие','Столики'],['Шкафы','Открытая'],['Минималистичная','Табурет'],['Туалетный','Для работы стоя'],['Для чтения','Качалка'],['Прислоненное','Зеркало'],['Настенная','Вешалка-камерди'],['Низкий комод','Корзины']],
            'lighting': [['Из сложенной','Металлический'],['Регулируемые','Деревянные'],['Стеклянная','Ретро-лампа'],['Трековое','LED-лента'],['Утопленные','Рассеивающий'],['Для чтения','На длинном'],['LED-профиль','Миниатюрные'],['Навигационный','Сенсорная'],['Рассеянный','Светящаяся']],
            'materials': [['Глубокоматовая','Вертикальные'],['Светлая сосна','Ковровое'],['Постельное','Органический'],['Шерстяные пледы','Стеганые'],['Детали','Детали'],['Льняные','Гладкие'],['Детали','Детали'],['База','Строгий'],['Тактильная','Гладкий']],
        },
        'bathroom': {
            'furniture': [['Парящая тумба','Бесшовная тумба'],['Отдельно','Душ Walk-in'],['Навесной','Открытые'],['Из массива тика','Откидное'],['Раковина','Керамическая'],['Круглое зеркало','Зеркало'],['Минималистичная','Подогреваемый'],['Унитаз со','Подвесное'],['Комод','Для белья']],
            'lighting': [['Мягкая','Тонкий'],['Встроенные','Накладные'],['Подвес','Бра'],['LED-подсветка','Направленный'],['Сенсорная','LED- подсветка'],['Яркий дневной','Теплый'],['Косметическое','Влагозащищенные'],['Свет','Большая'],['Подвесной','Настенный']],
            'materials': [['Плитка','Терраццо'],['Керамогранит','Керамогранит'],['Матовая белая','Брашированная'],['Реечный коврик','Плетеный'],['Стекло душевой','Рифленое'],['Поверхности','Натуральный'],['Гладкий','Панели'],['Монохромная','Палитра'],['Вафельные','Мягкие']],
        },
        'kitchen': {
            'furniture': [['Гладкий','Тёмный дуб'],['Гнутая фанера','Металл'],['Раздвижной','Белый лак'],['Полипропилен','Матовое стекло'],['Модульный','Белый матовый'],['Стремянка','Нержавейка'],['Гибкий излив','Нержавейка'],['Педальное','Никель'],['Перфорированная','Мрамор']],
            'lighting': [['Металл','Индустриал'],['Шарнирный','LED-лента'],['Эдисон','Точечные'],['Гладкий','Накладные'],['Микро-споты','Магнитный'],['Диммер','Гусиная шея'],['Грибок','Панель'],['LED-профиль','Сенсорный'],['Настенный','Плинтус']],
            'materials': [['Скинали','Zellige'],['Акрил','Мрамор'],['Фанера','Шалфей'],['Полимер','Хлопок'],['Скоба','Белая керамика'],['Шерсть','Терракота'],['Матовая','Латунь'],['Пробка','Бетон'],['Блэкаут','Лён']],
        },
    },
}


NEG = ('people, person, human figure, ugly, deformed, noisy, blurry, low resolution, '
       'oversaturated, flat lighting, text, watermark, logo, clutter, dark')

def app_keyboard():
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
            variant  = cat_setup.get(slot_key) or None  # None = unselected
            if not variant:
                selections.append((cat_id, n, None, '', ''))
                continue
            item     = get_item(catalog, style, room, cat_id, n, variant)
            name_ru  = item.get('name_ru', '')
            phrase   = item_phrase(item)
            selections.append((cat_id, n, variant, name_ru, phrase))
            if phrase:
                scene_parts.append(phrase)
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
        _style_labels = SLOT_LABELS.get(style, SLOT_LABELS.get('japandi', {}))
        slot_labels = _style_labels.get(room, {}).get(cat_id, [])
        _style_opts = SLOT_OPTS.get(style, SLOT_OPTS.get('japandi', {}))
        slot_opts   = _style_opts.get(room, {}).get(cat_id, [])
        cat_sels = [(n, v, nr, ph) for (c, n, v, nr, ph) in selections if c == cat_id]
        for n, variant, name_ru, phrase in cat_sels:
            label = slot_labels[n - 1] if n - 1 < len(slot_labels) else f'{px}_{n}'
            if not variant:
                lines.append(f'  {n:2}. {label:<26} —')
                continue
            vi       = 0 if variant == 'main' else 1
            var_name = slot_opts[n - 1][vi] if n - 1 < len(slot_opts) else ('A' if vi == 0 else 'Б')
            display  = name_ru if name_ru else var_name
            lines.append(f'  {n:2}. {label:<26} [{var_name}]  {display}')
        lines.append('')

    # ── Технические параметры ──────────────────────────────────
    lines += ['═' * W, '  ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ', '═' * W, '']
    lines.append(f'  Модель:    Juggernaut XL v9')
    lines.append(f'  Сэмплер:   Euler / Karras')
    lines.append(f'  Шаги:      30')
    lines.append(f'  CFG:       10')
    lines.append(f'  Seed:      {seed}')
    lines.append(f'  Размер:    1344×768')
    lines.append(f'  Время:     {elapsed_sec // 60}м {elapsed_sec % 60:02d}с')
    lines.append('')
    lines += ['─' * W, '  ПРОМТ (EN)', '─' * W]
    lines.append(prompt)
    lines.append('')
    lines += ['─' * W, '  НЕГАТИВНЫЙ ПРОМТ', '─' * W]
    lines.append(neg)
    lines += ['', '═' * W]
    return '\n'.join(lines)


def generate_room(chat_id: int, payload: dict, bot: telebot.TeleBot, username: str = ''):
    style = payload.get('style', '')
    room  = payload.get('room', '')
    setup = payload.get('setup', {})

    if style not in STYLE_DNA or room not in ROOM_DNA:
        bot.send_message(chat_id, '❌ Неизвестный стиль или комната.')
        return

    global _last_gen_time
    log_event(chat_id, username, 'gen_start', {'style': style, 'room': room})
    _last_gen_time = time.time()

    if not sd_ensure_up():
        bot.send_message(chat_id, '❌ SD WebUI не удалось запустить. Попробуй позже.')
        return

    catalog    = load_catalog()
    prompt, neg, selections = build_prompt_and_report(style, room, setup, catalog)

    bot.send_message(
        chat_id,
        f'🏠 Генерирую *{STYLE_RU[style]}* · *{ROOM_RU[room]}*\n'
        f'Это займёт 1–3 минуты, буду присылать обновления.',
        parse_mode='Markdown',
        reply_markup=app_keyboard(),
    )

    stop_event = threading.Event()
    start_ts   = time.time()

    def ticker():
        if stop_event.wait(50):
            return
        while not stop_event.is_set():
            elapsed = int(time.time() - start_ts)
            bot.send_message(chat_id, f'⏳ Генерация идёт… {elapsed // 60}м {elapsed % 60:02d}с')
            stop_event.wait(30)

    threading.Thread(target=ticker, daemon=True).start()

    try:
        r = requests.post(SD_URL, json={
            'prompt':                prompt,
            'negative_prompt':       neg,
            'steps':                 30,
            'cfg_scale':             10,
            'width':                 1344,
            'height':                768,
            'sampler_name':          'Euler',
            'scheduler':             'Karras',
            'seed':                  -1,
            'override_settings':     {'CLIP_stop_at_last_layers': 1},
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

        _last_gen_time = time.time()
        log_event(chat_id, username, 'gen_ok', {'style': style, 'room': room, 'elapsed': elapsed, 'seed': seed})

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
        log_event(chat_id, username, 'gen_fail', {'reason': 'timeout'})
        bot.send_message(chat_id, '❌ SD WebUI не ответил за 10 минут — попробуй ещё раз.')
    except Exception as e:
        stop_event.set()
        log_event(chat_id, username, 'gen_fail', {'reason': str(e)[:80]})
        bot.send_message(chat_id, f'❌ Ошибка: {str(e)[:200]}')


def main():
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

    def uname(message):
        return message.from_user.username or message.from_user.first_name or str(message.chat.id)

    @bot.message_handler(content_types=['web_app_data'])
    def on_web_app_data(message):
        un = uname(message)
        log_event(message.chat.id, un, 'webapp_data')
        try:
            payload = json.loads(message.web_app_data.data)
        except Exception:
            bot.send_message(message.chat.id, '❌ Не удалось разобрать данные из Mini App.')
            return

        threading.Thread(
            target=generate_room,
            args=(message.chat.id, payload, bot, un),
            daemon=True,
        ).start()

    APP_URL = 'https://tp9mc.github.io'

    def set_menu_button(chat_id=None):
        try:
            mb = telebot.types.MenuButtonCommands()
            if chat_id:
                bot.set_chat_menu_button(chat_id=chat_id, menu_button=mb)
            else:
                bot.set_chat_menu_button(menu_button=mb)
        except Exception:
            pass

    @bot.message_handler(commands=['app'])
    def on_app(message):
        log_event(message.chat.id, uname(message), 'app')
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(telebot.types.KeyboardButton(
            text='🏠 Открыть конструктор',
            web_app=telebot.types.WebAppInfo(url=APP_URL),
        ))
        bot.send_message(message.chat.id, '👇 Нажми чтобы открыть:', reply_markup=markup)

    @bot.message_handler(commands=['start', 'restart'])
    def on_start(message):
        log_event(message.chat.id, uname(message), message.text.split()[0].lstrip('/'))
        set_menu_button(message.chat.id)
        bot.send_message(
            message.chat.id,
            '👋 Привет! Нажми кнопку чтобы открыть конструктор.',
            reply_markup=app_keyboard(),
        )

    @bot.message_handler(commands=['stats'])
    def on_stats(message):
        log_event(message.chat.id, uname(message), 'stats')
        report = build_stats_report()
        buf = BytesIO(report.encode('utf-8'))
        buf.name = f'stats_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        bot.send_document(message.chat.id, buf, caption='📊 Статистика бота')

    bot.set_my_commands([
        telebot.types.BotCommand('/start',   '👋 Запустить бота'),
        telebot.types.BotCommand('/app',     '🏠 Открыть конструктор'),
        telebot.types.BotCommand('/stats',   '📊 Статистика'),
        telebot.types.BotCommand('/restart', '🔄 Перезапустить'),
    ])

    # Set globally (no chat_id) so button appears for all users without /start
    set_menu_button(None)

    print('Bot started, polling…')
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


if __name__ == '__main__':
    lock_path = '/tmp/propferma_bot.lock'
    try:
        lock_fd = open(lock_path, 'w')
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
    except OSError:
        print('Another instance is already running. Exiting.')
        raise SystemExit(1)
    main()
