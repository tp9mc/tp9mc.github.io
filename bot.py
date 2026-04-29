"""
Interior Constructor Bot
Receives web_app_data from Mini App, generates a room render via SD WebUI,
sends status updates every 30s, delivers image + full report to user.
"""
import json, re, threading, time, base64, requests, telebot, subprocess, signal, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from io import BytesIO
from PIL import Image
from datetime import datetime
from collections import defaultdict

from bot_secrets import BOT_TOKEN, HF_TOKEN, OWNER_CHAT_ID, OWNER_USERNAME, EDITOR_CHAT_IDS  # not committed to git
HF_URL        = 'https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell'
HF_TIMEOUT    = 120
HF_COST_PER_IMAGE = 0.0017  # $1.40 / 802 requests (Apr 2026)
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


# ── Local generation proxy (Mini App → bot → HF) ──────────────────────────
GEN_PROXY_PORT = 8765

REPO_DIR     = '/Users/timofeev_sd/claude-workspace/tp9mc.github.io'
SITE_URL     = 'https://tp9mc.github.io'
ASSETS_DIR   = os.path.join(REPO_DIR, 'custom_assets')


class _GenHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors(); self.end_headers()

    def do_POST(self):
        if self.path == '/publish':
            self._handle_publish()
        else:
            self._handle_generate()

    def _handle_generate(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body   = json.loads(self.rfile.read(length))
            prompt = str(body.get('prompt', '')).strip()
            w = min(int(body.get('width',  1024)), 1344)
            h = min(int(body.get('height', 1024)), 1344)
            if not prompt:
                return self._respond(400, b'{"error":"no prompt"}', 'application/json')
            r = requests.post(
                HF_URL,
                headers={'Authorization': f'Bearer {HF_TOKEN}'},
                json={'inputs': prompt, 'parameters': {'width': w, 'height': h}},
                timeout=HF_TIMEOUT,
            )
            if r.status_code != 200:
                raise RuntimeError(f'HF {r.status_code}: {r.text[:80]}')
            self._respond(200, r.content, 'image/jpeg')
        except Exception as e:
            self._respond(500, json.dumps({'error': str(e)[:120]}).encode(), 'application/json')

    def _handle_publish(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            data   = json.loads(self.rfile.read(length))
            edits  = data.get('edits', {})
            imgs   = data.get('imgs',  {})

            os.makedirs(ASSETS_DIR, exist_ok=True)

            # Save data-URL images as real files; keep only metadata + new URL
            clean_imgs = {}
            for eid, item in imgs.items():
                url = item.get('url', '')
                clean = {k: v for k, v in item.items() if k != 'url'}
                if url.startswith('data:image'):
                    _, b64 = url.split(',', 1)
                    fname = eid.replace('/', '_') + '.jpg'
                    with open(os.path.join(ASSETS_DIR, fname), 'wb') as f:
                        f.write(base64.b64decode(b64))
                    clean['url'] = f'{SITE_URL}/custom_assets/{fname}'
                elif url:
                    clean['url'] = url
                if clean:
                    clean_imgs[eid] = clean

            site_edits_path = os.path.join(REPO_DIR, 'site_edits.json')
            with open(site_edits_path, 'w', encoding='utf-8') as f:
                json.dump({'edits': edits, 'imgs': clean_imgs}, f,
                          ensure_ascii=False, indent=2)

            result = subprocess.run(
                'git add site_edits.json custom_assets/ '
                '&& git diff --cached --quiet '
                '|| git commit -m "site: publish editor changes" '
                '&& git push origin main',
                shell=True, cwd=REPO_DIR, capture_output=True, text=True,
            )
            ok = result.returncode == 0
            changes = data.get('changes', [])
            tg_user = data.get('user') or {}
            pub_user_id = int(tg_user.get('id') or OWNER_CHAT_ID)
            pub_username = str(tg_user.get('username') or tg_user.get('first_name') or OWNER_USERNAME)
            if ok:
                log_event(pub_user_id, pub_username, 'site_publish', {
                    'texts':   len(edits),
                    'images':  len(clean_imgs),
                    'changes': changes,
                })
            self._respond(200, json.dumps({'ok': ok}).encode(), 'application/json')
        except Exception as e:
            self._respond(500, json.dumps({'error': str(e)[:200]}).encode(), 'application/json')

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def _respond(self, code, body, ctype):
        self.send_response(code)
        self._cors()
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _start_gen_proxy():
    HTTPServer(('127.0.0.1', GEN_PROXY_PORT), _GenHandler).serve_forever()


threading.Thread(target=_start_gen_proxy, daemon=True).start()


# ── Cloudflare quick tunnel (public HTTPS URL for the proxy) ──────────────
_tunnel_url: str | None = None
_PENDING_DEL_PATH = '/tmp/propferma_pending_del.json'

def _save_pending_del(chat_id, message_id):
    try:
        data = []
        try:
            with open(_PENDING_DEL_PATH) as f: data = json.load(f)
        except Exception: pass
        data.append({'c': chat_id, 'm': message_id})
        with open(_PENDING_DEL_PATH, 'w') as f: json.dump(data, f)
    except Exception: pass

def _flush_pending_del(bot_ref):
    try:
        with open(_PENDING_DEL_PATH) as f: data = json.load(f)
        os.remove(_PENDING_DEL_PATH)
        for entry in data:
            try: bot_ref.delete_message(entry['c'], entry['m'])
            except Exception: pass
    except Exception: pass


def _start_tunnel(bot_ref=None):
    """Start cloudflared quick tunnel, parse URL, optionally notify via Telegram."""
    global _tunnel_url
    try:
        proc = subprocess.Popen(
            ['cloudflared', 'tunnel', '--url', f'http://localhost:{GEN_PROXY_PORT}',
             '--no-autoupdate'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            m = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
            if m:
                _tunnel_url = m.group(0)
                print(f'[tunnel] {_tunnel_url}')
                if bot_ref:
                    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
                    markup.add(telebot.types.KeyboardButton(
                        text='✏️ Открыть редактор',
                        web_app=telebot.types.WebAppInfo(
                            url=f'https://tp9mc.github.io?proxy={_tunnel_url}'
                        ),
                    ))
                    for cid in EDITOR_CHAT_IDS:
                        try:
                            msg = bot_ref.send_message(
                                cid,
                                '✏️ Редактор готов',
                                reply_markup=markup,
                                disable_notification=True,
                            )
                            _save_pending_del(cid, msg.message_id)
                            def _del(c=cid, m=msg.message_id):
                                time.sleep(30)
                                try: bot_ref.delete_message(c, m)
                                except Exception: pass
                            threading.Thread(target=_del, daemon=True).start()
                        except Exception:
                            pass
                break
        proc.wait()
    except FileNotFoundError:
        print('[tunnel] cloudflared not found, skipping tunnel')
    except Exception as e:
        print(f'[tunnel] error: {e}')

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
    hf_count     = 0
    sd_count     = 0

    for e in events:
        uid  = e['user_id']
        uname = e.get('username', str(uid))
        if uid not in users:
            users[uid] = {'username': uname, 'actions': defaultdict(int), 'gens_hf': 0, 'gens_sd': 0, 'first': e['ts']}
        users[uid]['actions'][e['action']] += 1
        users[uid]['last'] = e['ts']

        if e['action'] == 'gen_ok':
            gen_ok += 1
            style_count[e.get('style', '?')] += 1
            room_count[e.get('room', '?')]   += 1
            if 'elapsed' in e:
                gen_times.append(e['elapsed'])
            if e.get('backend', 'hf') == 'hf':
                hf_count += 1
                users[uid]['gens_hf'] += 1
            else:
                sd_count += 1
                users[uid]['gens_sd'] += 1
        elif e['action'] == 'gen_fail':
            gen_fail += 1

    publish_events = [e for e in events if e['action'] == 'site_publish']

    W = 62
    lines = []
    lines += ['═' * W, '  СТАТИСТИКА БОТА', '═' * W, '']
    lines.append(f'  Дата отчёта:   {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'  Всего событий: {len(events)}')
    lines.append(f'  Уникальных пользователей: {len(users)}')
    lines.append(f'  Генераций успешных: {gen_ok}  (HF: {hf_count}, SD: {sd_count})')
    lines.append(f'  Генераций с ошибкой: {gen_fail}')
    if gen_times:
        avg = sum(gen_times) / len(gen_times)
        lines.append(f'  Среднее время генерации: {int(avg)//60}м {int(avg)%60:02d}с')
    hf_total = hf_count * HF_COST_PER_IMAGE
    lines.append(f'  Расходы HF (бот): ${hf_total:.4f}  (×${HF_COST_PER_IMAGE:.4f}/генерация)')
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
        gens     = acts.get('gen_ok', 0)
        gens_hf  = u.get('gens_hf', 0)
        gens_sd  = u.get('gens_sd', 0)
        cost_hf  = gens_hf * HF_COST_PER_IMAGE
        lines.append(f'  @{u["username"]} (id {uid})')
        lines.append(f'    Всего действий: {total_acts}  |  Генераций: {gens}  (HF: {gens_hf}, SD: {gens_sd})')
        lines.append(f'    Расходы HF: ${cost_hf:.4f}')
        lines.append(f'    Первое: {u["first"]}  |  Последнее: {u.get("last","")}')
        details = ', '.join(f'{k}={v}' for k, v in sorted(acts.items()))
        lines.append(f'    Действия: {details}')
        lines.append('')

    if publish_events:
        lines += ['─' * W, '  ИЗМЕНЕНИЯ НА САЙТЕ', '─' * W]
        TYPE_RU = {
            'text':   'Текст',
            'prompt': 'Промт',
            'upload': 'Картинка (загружена)',
            'gen':    'Картинка (генерация)',
        }
        for e in reversed(publish_events):
            changes = e.get('changes', [])
            pub_who = f'@{e.get("username","?")}  '
            lines.append(f'  {e["ts"]}  {pub_who}({len(changes)} изм.)')
            for c in changes:
                t    = TYPE_RU.get(c.get('type',''), c.get('type',''))
                eid  = c.get('eid', '')
                val  = c.get('value', '')
                if val:
                    short = val[:60] + ('…' if len(val) > 60 else '')
                    lines.append(f'    • {t}: [{eid}] → {short}')
                else:
                    lines.append(f'    • {t}: [{eid}]')
            lines.append('')

    lines += ['─' * W, '  ПОЛНЫЙ ЛОГ СОБЫТИЙ (новые сверху)', '─' * W]
    for e in reversed(events):
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


def build_report(style, room, setup, selections, prompt, neg, seed, elapsed_sec, backend='hf'):
    W = 42
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
                lines.append(f'  {n:2}. {label}')
                lines.append(f'      — не выбрано')
                continue
            vi       = 0 if variant == 'main' else 1
            var_name = slot_opts[n - 1][vi] if n - 1 < len(slot_opts) else ('A' if vi == 0 else 'Б')
            display  = name_ru if name_ru else var_name
            lines.append(f'  {n:2}. {label}')
            lines.append(f'      ↳ [{var_name}] {display}')
        lines.append('')

    # ── Технические параметры ──────────────────────────────────
    lines += ['═' * W, '  ТЕХНИЧЕСКИЕ ПАРАМЕТРЫ', '═' * W, '']
    if backend == 'hf':
        lines.append(f'  Модель:    FLUX.1-schnell')
        lines.append(f'  Провайдер: HuggingFace')
        lines.append(f'  Размер:    1344×768')
        lines.append(f'  Время:     {elapsed_sec // 60}м {elapsed_sec % 60:02d}с')
        lines.append(f'  Стоимость: ${HF_COST_PER_IMAGE:.4f}')
        lines.append('')
        lines += ['─' * W, '  ПРОМТ', '─' * W]
        lines.append(prompt)
    else:
        lines.append(f'  Модель:    Juggernaut XL v9')
        lines.append(f'  Сэмплер:   Euler / Karras')
        lines.append(f'  Шаги:      30  CFG: 10')
        lines.append(f'  Seed:      {seed}')
        lines.append(f'  Размер:    1344×768')
        lines.append(f'  Время:     {elapsed_sec // 60}м {elapsed_sec % 60:02d}с')
        lines.append(f'  Стоимость: $0.00 (локально)')
        lines.append('')
        lines += ['─' * W, '  ПРОМТ', '─' * W]
        lines.append(prompt)
        lines.append('')
        lines += ['─' * W, '  НЕГАТИВНЫЙ ПРОМТ', '─' * W]
        lines.append(neg)
    lines += ['', '═' * W]
    return '\n'.join(lines)


def _generate_hf(prompt: str) -> bytes:
    """Generate image via HuggingFace FLUX.1-schnell. Returns raw image bytes."""
    r = requests.post(
        HF_URL,
        headers={'Authorization': f'Bearer {HF_TOKEN}'},
        json={'inputs': prompt, 'parameters': {'width': 1344, 'height': 768}},
        timeout=HF_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f'HF {r.status_code}: {r.text[:120]}')
    return r.content


def _generate_sd(prompt: str, neg: str) -> bytes:
    """Generate image via local SD WebUI. Returns raw image bytes."""
    r = requests.post(SD_URL, json={
        'prompt': prompt, 'negative_prompt': neg,
        'steps': 30, 'cfg_scale': 10,
        'width': 1344, 'height': 768,
        'sampler_name': 'Euler', 'scheduler': 'Karras',
        'seed': -1, 'override_settings': {'CLIP_stop_at_last_layers': 1},
    }, timeout=SD_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'SD {r.status_code}')
    return base64.b64decode(r.json()['images'][0])


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

    catalog = load_catalog()
    prompt, neg, selections = build_prompt_and_report(style, room, setup, catalog)

    bot.send_message(
        chat_id,
        f'🏠 Генерирую *{STYLE_RU[style]}* · *{ROOM_RU[room]}*…',
        parse_mode='Markdown',
        reply_markup=app_keyboard(),
    )

    start_ts = time.time()
    img_bytes = None
    backend   = 'hf'

    # Primary: HuggingFace FLUX.1-schnell
    try:
        img_bytes = _generate_hf(prompt)
    except Exception as e:
        hf_err = str(e)[:80]
        # Fallback: local SD WebUI
        backend = 'sd'
        try:
            if not sd_ensure_up():
                raise RuntimeError('SD WebUI не удалось запустить')
            img_bytes = _generate_sd(prompt, neg)
        except Exception as e2:
            log_event(chat_id, username, 'gen_fail', {'reason': f'hf={hf_err} sd={str(e2)[:60]}'})
            bot.send_message(chat_id, f'❌ Не удалось сгенерировать.\nHF: {hf_err}\nSD: {str(e2)[:100]}')
            return

    elapsed = int(time.time() - start_ts)
    _last_gen_time = time.time()

    img = Image.open(BytesIO(img_bytes)).convert('RGB')
    img_buf = BytesIO()
    img.save(img_buf, 'JPEG', quality=92)
    img_buf.seek(0)

    log_event(chat_id, username, 'gen_ok', {'style': style, 'room': room, 'elapsed': elapsed, 'backend': backend})

    bot.send_photo(
        chat_id, img_buf,
        caption=f'✅ Готово за {elapsed}с\n*{STYLE_RU[style]}* · *{ROOM_RU[room]}*',
        parse_mode='Markdown',
    )

    report_text = build_report(style, room, setup, selections, prompt, neg, -1, elapsed, backend)
    report_buf  = BytesIO(report_text.encode('utf-8'))
    report_buf.name = f'report_{style}_{room}.txt'
    bot.send_document(chat_id, report_buf, caption='📄 Параметры генерации')


def main():
    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)

    _flush_pending_del(bot)

    threading.Thread(
        target=_start_tunnel, kwargs={'bot_ref': bot},
        daemon=True,
    ).start()

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

        if payload.get('type') == 'publish':
            def _do_publish():
                data = payload
                edits  = data.get('edits', {})
                imgs   = data.get('imgs',  {})
                changes = data.get('changes', [])
                os.makedirs(ASSETS_DIR, exist_ok=True)
                site_edits_path = os.path.join(REPO_DIR, 'site_edits.json')
                # sendData sends imgs:{} — preserve existing custom images
                if not imgs:
                    try:
                        with open(site_edits_path, 'r', encoding='utf-8') as f:
                            imgs = json.load(f).get('imgs', {})
                    except Exception:
                        imgs = {}
                with open(site_edits_path, 'w', encoding='utf-8') as f:
                    json.dump({'edits': edits, 'imgs': imgs}, f, ensure_ascii=False, indent=2)
                result = subprocess.run(
                    'git add site_edits.json custom_assets/ '
                    '&& git diff --cached --quiet '
                    '|| git commit -m "site: publish editor changes" '
                    '&& git push origin main',
                    shell=True, cwd=REPO_DIR, capture_output=True, text=True,
                )
                if result.returncode == 0:
                    log_event(message.chat.id, un, 'site_publish', {
                        'texts':   len(edits),
                        'images':  len(imgs),
                        'changes': changes,
                    })
                    bot.send_message(message.chat.id, '✅ Изменения опубликованы на сайте!')
                else:
                    bot.send_message(message.chat.id, '❌ Ошибка публикации.')
            threading.Thread(target=_do_publish, daemon=True).start()
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
    bot.infinity_polling(timeout=30, long_polling_timeout=20, interval=5, skip_pending=True)


if __name__ == '__main__':
    import fcntl
    lock_path = '/tmp/propferma_bot.lock'
    # Check if a running PID is already in the lock file before taking the lock.
    # This survives stale-file situations created by `rm` of the old lock.
    try:
        with open(lock_path) as _f:
            _old_pid = int(_f.read().strip())
        try:
            os.kill(_old_pid, 0)          # signal 0 = just probe
            print(f'Another instance is already running (PID {_old_pid}). Exiting.')
            raise SystemExit(1)
        except ProcessLookupError:
            pass                          # stale PID — safe to continue
    except (FileNotFoundError, ValueError):
        pass                              # no lock file or empty — safe to continue
    lock_fd = open(lock_path, 'w')
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    main()
