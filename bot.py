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

from bot_secrets import BOT_TOKEN, HF_TOKEN, OWNER_CHAT_ID, OWNER_USERNAME, EDITOR_CHAT_IDS, GH_PUBLISH_TOKEN  # not committed to git
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
            # stats_only=True: frontend already published via GitHub API, just log
            if data.get('stats_only'):
                changes  = data.get('changes', [])
                tg_user  = data.get('user') or {}
                uid      = int(tg_user.get('id') or OWNER_CHAT_ID)
                uname    = str(tg_user.get('username') or tg_user.get('first_name') or OWNER_USERNAME)
                log_event(uid, uname, 'site_publish', {'texts': 0, 'images': 0, 'changes': changes})
                return self._respond(200, b'{"ok":true}', 'application/json')
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


def _poll_gh_publishes():
    """Detect GitHub-API publishes by polling commits to site_edits.json."""
    last_sha = None
    while True:
        time.sleep(300)
        try:
            r = requests.get(
                'https://api.github.com/repos/tp9mc/tp9mc.github.io/commits'
                '?path=site_edits.json&per_page=1',
                headers={'Authorization': f'token {GH_PUBLISH_TOKEN}'},
                timeout=10,
            )
            if r.status_code != 200:
                continue
            commits = r.json()
            if not commits:
                continue
            sha = commits[0]['sha']
            if last_sha is None:
                last_sha = sha
                continue
            if sha == last_sha:
                continue
            last_sha = sha
            msg = commits[0]['commit']['message']
            # "site: publish by @Username"
            m = re.search(r'@(\S+)', msg)
            author = m.group(1) if m else 'editor'
            log_event(OWNER_CHAT_ID, author, 'site_publish', {
                'texts': 0, 'images': 0, 'changes': [],
                'via': 'github_api', 'sha': sha[:8],
            })
        except Exception:
            pass


threading.Thread(target=_poll_gh_publishes, daemon=True).start()


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
                            url=f'https://tp9mc.github.io?proxy={_tunnel_url}&t={GH_PUBLISH_TOKEN}&u=editor'
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
                                time.sleep(900)
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
CATS     = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm'), ('humor', 'h')]
CAT_RU   = {"furniture": "Мебель", "lighting": "Освещение", "materials": "Материалы", "humor": "Юмор"}

# Russian slot labels per room (matches ROOM_LABELS in index.html)
SLOT_LABELS = {
    "japandi": {
        "living": {
            "furniture": [
                "Диван",
                "Кресло",
                "Журнальный стол",
                "Приставной столик",
                "ТВ-зона",
                "Стеллаж",
                "Комод/буфет",
                "Консоль",
                "Пуф"
            ],
            "lighting": [
                "Люстра",
                "Подвес",
                "Торшер",
                "Настольная",
                "Бра",
                "Споты",
                "LED-карниз",
                "Подсветка картин",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Ковёр",
                "Шторы",
                "Обивка",
                "Подушки/плед",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bedroom": {
            "furniture": [
                "Кровать",
                "Изголовье",
                "Прикроватная тумба",
                "Комод",
                "Шкаф",
                "Банкетка",
                "Туалетный столик",
                "Кресло для чтения",
                "Зеркало"
            ],
            "lighting": [
                "Потолочный",
                "Подвес",
                "Прикроватная",
                "Бра для чтения",
                "Споты",
                "LED-изголовье",
                "Свет в шкафу",
                "Торшер",
                "Ночник"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Постельный текстиль",
                "Ковёр",
                "Шторы blackout",
                "Обивка",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bathroom": {
            "furniture": [
                "Тумба",
                "Раковина",
                "Ванна",
                "Душевая",
                "Унитаз",
                "Пенал",
                "Зеркало-шкаф",
                "Банкетка",
                "Полотенцедержатель"
            ],
            "lighting": [
                "Потолочный",
                "Свет зеркала",
                "Споты",
                "Свет ниши",
                "Бра",
                "Подсветка зеркала",
                "LED-карниз",
                "Ночник",
                "Диммер"
            ],
            "materials": [
                "Плитка пола",
                "Плитка стен",
                "Отделка душа",
                "Потолок",
                "Столешница",
                "Смеситель-финиш",
                "Полотенца",
                "Коврик",
                "Стекло душа"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "kitchen": {
            "furniture": [
                "Нижние шкафы",
                "Верхние шкафы",
                "Остров",
                "Пенал",
                "Барные стулья",
                "Обеденный стол",
                "Стулья",
                "Мойка+смеситель",
                "Открытые полки"
            ],
            "lighting": [
                "Общий",
                "Подвес над островом",
                "Подсветка рабочей зоны",
                "Споты",
                "Трек",
                "Подсветка цоколя",
                "Свет в шкафах",
                "Обеденный подвес",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Столешница",
                "Фартук",
                "Фасады",
                "Стены",
                "Ручки",
                "Материал мойки",
                "Шторы",
                "Текстиль"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        }
    },
    "modern_classic": {
        "living": {
            "furniture": [
                "Диван",
                "Кресло",
                "Журнальный стол",
                "Приставной столик",
                "ТВ-зона",
                "Стеллаж",
                "Комод/буфет",
                "Консоль",
                "Пуф"
            ],
            "lighting": [
                "Люстра",
                "Подвес",
                "Торшер",
                "Настольная",
                "Бра",
                "Споты",
                "LED-карниз",
                "Подсветка картин",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Ковёр",
                "Шторы",
                "Обивка",
                "Подушки/плед",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bedroom": {
            "furniture": [
                "Кровать",
                "Изголовье",
                "Прикроватная тумба",
                "Комод",
                "Шкаф",
                "Банкетка",
                "Туалетный столик",
                "Кресло для чтения",
                "Зеркало"
            ],
            "lighting": [
                "Потолочный",
                "Подвес",
                "Прикроватная",
                "Бра для чтения",
                "Споты",
                "LED-изголовье",
                "Свет в шкафу",
                "Торшер",
                "Ночник"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Постельный текстиль",
                "Ковёр",
                "Шторы blackout",
                "Обивка",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bathroom": {
            "furniture": [
                "Тумба",
                "Раковина",
                "Ванна",
                "Душевая",
                "Унитаз",
                "Пенал",
                "Зеркало-шкаф",
                "Банкетка",
                "Полотенцедержатель"
            ],
            "lighting": [
                "Потолочный",
                "Свет зеркала",
                "Споты",
                "Свет ниши",
                "Бра",
                "Подсветка зеркала",
                "LED-карниз",
                "Ночник",
                "Диммер"
            ],
            "materials": [
                "Плитка пола",
                "Плитка стен",
                "Отделка душа",
                "Потолок",
                "Столешница",
                "Смеситель-финиш",
                "Полотенца",
                "Коврик",
                "Стекло душа"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "kitchen": {
            "furniture": [
                "Нижние шкафы",
                "Верхние шкафы",
                "Остров",
                "Пенал",
                "Барные стулья",
                "Обеденный стол",
                "Стулья",
                "Мойка+смеситель",
                "Открытые полки"
            ],
            "lighting": [
                "Общий",
                "Подвес над островом",
                "Подсветка рабочей зоны",
                "Споты",
                "Трек",
                "Подсветка цоколя",
                "Свет в шкафах",
                "Обеденный подвес",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Столешница",
                "Фартук",
                "Фасады",
                "Стены",
                "Ручки",
                "Материал мойки",
                "Шторы",
                "Текстиль"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        }
    },
    "scandi": {
        "living": {
            "furniture": [
                "Диван",
                "Кресло",
                "Журнальный стол",
                "Приставной столик",
                "ТВ-зона",
                "Стеллаж",
                "Комод/буфет",
                "Консоль",
                "Пуф"
            ],
            "lighting": [
                "Люстра",
                "Подвес",
                "Торшер",
                "Настольная",
                "Бра",
                "Споты",
                "LED-карниз",
                "Подсветка картин",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Ковёр",
                "Шторы",
                "Обивка",
                "Подушки/плед",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bedroom": {
            "furniture": [
                "Кровать",
                "Изголовье",
                "Прикроватная тумба",
                "Комод",
                "Шкаф",
                "Банкетка",
                "Туалетный столик",
                "Кресло для чтения",
                "Зеркало"
            ],
            "lighting": [
                "Потолочный",
                "Подвес",
                "Прикроватная",
                "Бра для чтения",
                "Споты",
                "LED-изголовье",
                "Свет в шкафу",
                "Торшер",
                "Ночник"
            ],
            "materials": [
                "Пол",
                "Стены",
                "Акцентная стена",
                "Потолок",
                "Постельный текстиль",
                "Ковёр",
                "Шторы blackout",
                "Обивка",
                "Фурнитура"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "bathroom": {
            "furniture": [
                "Тумба",
                "Раковина",
                "Ванна",
                "Душевая",
                "Унитаз",
                "Пенал",
                "Зеркало-шкаф",
                "Банкетка",
                "Полотенцедержатель"
            ],
            "lighting": [
                "Потолочный",
                "Свет зеркала",
                "Споты",
                "Свет ниши",
                "Бра",
                "Подсветка зеркала",
                "LED-карниз",
                "Ночник",
                "Диммер"
            ],
            "materials": [
                "Плитка пола",
                "Плитка стен",
                "Отделка душа",
                "Потолок",
                "Столешница",
                "Смеситель-финиш",
                "Полотенца",
                "Коврик",
                "Стекло душа"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        },
        "kitchen": {
            "furniture": [
                "Нижние шкафы",
                "Верхние шкафы",
                "Остров",
                "Пенал",
                "Барные стулья",
                "Обеденный стол",
                "Стулья",
                "Мойка+смеситель",
                "Открытые полки"
            ],
            "lighting": [
                "Общий",
                "Подвес над островом",
                "Подсветка рабочей зоны",
                "Споты",
                "Трек",
                "Подсветка цоколя",
                "Свет в шкафах",
                "Обеденный подвес",
                "Диммер"
            ],
            "materials": [
                "Пол",
                "Столешница",
                "Фартук",
                "Фасады",
                "Стены",
                "Ручки",
                "Материал мойки",
                "Шторы",
                "Текстиль"
            ],
            "humor": [
                "Абсурдный масштаб",
                "Звериный захват",
                "Анахронизм",
                "Совковый мем",
                "Буквальная идиома",
                "Подмена функции",
                "Еда как мебель",
                "Поп-абсурд",
                "Проклятая комбинация"
            ]
        }
    }
}

SLOT_OPTS = {
    "japandi": {
        "living": {
            "furniture": [
                [
                    "Светлый дуб, льняные подушки",
                    "Тёплый орех, букле",
                    "Наклонный профиль, асимметрия"
                ],
                [
                    "Лёгкий дуб, льняной",
                    "Орех, букле, бронза",
                    "Наклонные ножки, инлей"
                ],
                [
                    "Прямоугольный, дуб, травертин",
                    "Орех, травертин, бронза",
                    "Овальный, дуб, подиум"
                ],
                [
                    "Прямой, дуб, черные ножки",
                    "Орех, бронза, ручка",
                    "Круглый, дуб, полоска"
                ],
                [
                    "Дуб, льняные панели, черный металл",
                    "Орех, букле, бронза",
                    "Плавающий, дуб, открытые полки"
                ],
                [
                    "Дуб, льняные рейки, черный кронштейн",
                    "Орех, букле, бронзовый кронштейн",
                    "Лестничный, дуб, терракотовый маркер"
                ],
                [
                    "Дуб, льняные двери, черные ручки",
                    "Орех, букле, бронзовые детали",
                    "Тонкий, дуб, раздвижной букле"
                ],
                [
                    "Дуб, льняный бегунок, черные ножки",
                    "Орех, букле, бронзовые ножки",
                    "L‑образный, дуб, терракотовый угол"
                ],
                [
                    "Дуб, льняная обивка, черный сталь",
                    "Орех, букле, бронза",
                    "Круглый, дуб, радиальный терракот"
                ]
            ],
            "lighting": [
                [
                    "Матовый сталь",
                    "Терракотовый акцент",
                    "Низкопрофильный"
                ],
                [
                    "Круглый держатель",
                    "Круглый держатель",
                    "Линейный"
                ],
                [
                    "Трёхногий ствол",
                    "Трёхногий ствол",
                    "Кривой"
                ],
                [
                    "Ствол из стали",
                    "Ствол из стали",
                    "Цилиндрический"
                ],
                [
                    "Крепление из стали",
                    "Крепление из стали",
                    "Небольшой"
                ],
                [
                    "Плитка из стали",
                    "Плитка из стали",
                    "Группа"
                ],
                [
                    "Кроватка из стали",
                    "Кроватка из стали",
                    "Пошаговый"
                ],
                [
                    "Крепление из стали",
                    "Крепление из стали",
                    "Двойное"
                ],
                [
                    "Панель из стали",
                    "Панель из стали",
                    "Круглая"
                ]
            ],
            "materials": [
                [
                    "Светлый дуб",
                    "Тёмный орех",
                    "Диагональный орнамент"
                ],
                [
                    "Гладкая текстура",
                    "Щетиновая текстура",
                    "Рельефная текстура"
                ],
                [
                    "Вертикальная полоска",
                    "Горизонтальная полоска",
                    "Диагональная полоска"
                ],
                [
                    "Гладкая поверхность",
                    "Песчаная текстура",
                    "Рельефная полоска"
                ],
                [
                    "Прямоугольный лен",
                    "Круглый лен",
                    "Ковёр с бахромой"
                ],
                [
                    "Прямой лен",
                    "Плюшевый бублик",
                    "Плиссированный лен"
                ],
                [
                    "Прямой лен",
                    "Плюшевый бублик",
                    "Диагональный отрезок"
                ],
                [
                    "Лен + бублик",
                    "Бублик + лен",
                    "Слойчатый набор"
                ],
                [
                    "Чёрный металл",
                    "Бронзовый патин",
                    "Л‑образный уголок"
                ]
            ],
            "humor": [
                [
                    "Гладкий морской камень",
                    "Книга-диван",
                    "Телепульт гигант"
                ],
                [
                    "Трон-кровать с балдахином",
                    "Гамак-трон с кисточками",
                    "Замок-кондо с башней"
                ],
                [
                    "Подставка для пульта из доспехов",
                    "Вешалка для мечей вместо полки",
                    "Шлем-хранилище для пульта"
                ],
                [
                    "Коврик с оленем",
                    "Плакат-полка",
                    "Советский кухонный шкаф"
                ],
                [
                    "Слон в комнате",
                    "Лиса на пьедестале",
                    "Рыба в бассейне"
                ],
                [
                    "Гамак между колоннами",
                    "Шезлонг-качели",
                    "Парящее кресло-платформа"
                ],
                [
                    "Кресло-авокадо",
                    "Шезлонг-апельсин",
                    "Диван-ролл"
                ],
                [
                    "Игровой автомат с когтями",
                    "Музыкальный автомат-буфет",
                    "Пинбол-стол"
                ],
                [
                    "Будда в VR-очках",
                    "Зен-сад с геймпадом",
                    "Кот на книжной полке"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Светлый дуб, зелёный акцент",
                    "Пепельный ясень, латунный штрих",
                    "Широкий профиль, асимметричные ножки"
                ],
                [
                    "Ленивый тканевый покрывало",
                    "Лён с латунной гвоздкой",
                    "Узкий наклон, чёрные скобы"
                ],
                [
                    "Дубовый топ, травертин ящик",
                    "Ясень, латунная ручка",
                    "Парящий топ, чёрная опора"
                ],
                [
                    "Дубовый корпус, лён‑ручки",
                    "Ясень, льняной плед, латунь",
                    "Неровные ящики, чёрный сталь"
                ],
                [
                    "Дуб, чёрные направляющие",
                    "Ясень, латунные направляющие",
                    "Асимметричные двери, сталь"
                ],
                [
                    "Дубовые планки, сардовый кант",
                    "Ясень, латунные наконечники",
                    "Изогнутая спинка, чёрный базис"
                ],
                [
                    "Дубовый верх, травертин, чёрные тянущие",
                    "Ясень, латунные тянущие, льняной плед",
                    "Смещённый топ, стальная фурнитура"
                ],
                [
                    "Ясень, льняная обивка, чёрные ножки",
                    "Дуб, латунный гвоздь, льняной плед",
                    "Наклон спинки, стальная база"
                ],
                [
                    "Дубовая рама, сардовая полоска",
                    "Ясень, латунный край",
                    "Асимметричный угол, сталь"
                ]
            ],
            "lighting": [
                [
                    "Ольховый базовый",
                    "Ясеневый базовый",
                    "Выше профиль"
                ],
                [
                    "Ольховое кольцо",
                    "Ясеневый кольцо",
                    "Шире кольцо"
                ],
                [
                    "Ольховый фундамент",
                    "Ясеневый фундамент",
                    "Выше светильник"
                ],
                [
                    "Ольховая рука",
                    "Ясеневый рукав",
                    "Широкий рукав"
                ],
                [
                    "Ольховый корпус",
                    "Ясеневый корпус",
                    "Высокий корпус"
                ],
                [
                    "Ольховый профиль",
                    "Ясеневый профиль",
                    "Большой профиль"
                ],
                [
                    "Ольховый корпус",
                    "Ясеневый корпус",
                    "Повышенный корпус"
                ],
                [
                    "Ольховый подиум",
                    "Ясеневый подиум",
                    "Выше подиум"
                ],
                [
                    "Ольховый фундамент",
                    "Ясеневый фундамент",
                    "Широкий фундамент"
                ]
            ],
            "materials": [
                [
                    "Натуральный дуб",
                    "Бледный ясень",
                    "Кромка дерева"
                ],
                [
                    "Песочный лимо",
                    "Бежевый гипс",
                    "Палка кисти"
                ],
                [
                    "Бесцветный фон",
                    "Вертикальная полоска",
                    "Диагональная лента"
                ],
                [
                    "Плоский гипс",
                    "Тёплый лимо",
                    "Квадратный панель"
                ],
                [
                    "Песчаный льняной",
                    "Каштановый плед",
                    "Складной комплект"
                ],
                [
                    "Песчаный ковёр",
                    "Шёлковый плед",
                    "Круглый ковёр"
                ],
                [
                    "Песчаный штор",
                    "Каштановый плед",
                    "Складной панель"
                ],
                [
                    "Песчаный обив",
                    "Каштановый плед",
                    "Кромка ткани"
                ],
                [
                    "Чёрный металл",
                    "Жёлтый латунь",
                    "Изогнутый ручка"
                ]
            ],
            "humor": [
                [
                    "Дуговой циферблат‑кровать",
                    "Круглая коробка‑кресло",
                    "Экран‑платформа кровать"
                ],
                [
                    "Кошачья канапе‑кровать",
                    "Кошачий шоджи‑перекладина",
                    "Кроличий лофт‑люк"
                ],
                [
                    "Доспех‑приставка",
                    "Шлем‑ночник",
                    "Щит‑вешалка"
                ],
                [
                    "Ковровый стеновой изголовок",
                    "Книжный шкаф‑татарми",
                    "Бетон‑экран‑занавес"
                ],
                [
                    "Бревно‑кровать",
                    "Ствол‑тумбочка",
                    "Планка‑кровать"
                ],
                [
                    "Шар‑котелок‑кровать",
                    "Песочница‑матрас",
                    "Скользящая горка‑кровать"
                ],
                [
                    "Яичный кровать",
                    "Блин‑кровать",
                    "Круассан‑кровать"
                ],
                [
                    "Диско‑шар‑люстра",
                    "Телевизор‑лампа",
                    "Винил‑светильник"
                ],
                [
                    "Олень‑голова‑надголовье",
                    "Кит‑череп‑надголовье",
                    "Бабочки‑крышка‑кровать"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Тёплые тиковые планки",
                    "Оксидные дубовые двери",
                    "Плавающая низкая тумба"
                ],
                [
                    "Матовый чёрный обод",
                    "Травертиновый квадрат",
                    "Овальный травертин"
                ],
                [
                    "Тиковые боковины",
                    "Дубовые боковины",
                    "Таперные ножки"
                ],
                [
                    "Тиковый каркас",
                    "Дубовый каркас",
                    "Рецессивный тиковый профиль"
                ],
                [
                    "Тиковое сиденье",
                    "Дубовое сиденье",
                    "Таперные ножки"
                ],
                [
                    "Тиковые створки",
                    "Дубовые створки",
                    "Асимметричные тиковые створки"
                ],
                [
                    "Тиковая рама",
                    "Дубовая рама",
                    "Прямоугольный тиковый профиль"
                ],
                [
                    "Тиковый сиденье",
                    "Дубовое сиденье",
                    "Изогнутый тиковый дизайн"
                ],
                [
                    "Тиковые ступени",
                    "Дубовые ступени",
                    "Наклонный тиковый дизайн"
                ]
            ],
            "lighting": [
                [
                    "Тик, отделка",
                    "Дуб, отделка",
                    "Круглая форма"
                ],
                [
                    "Тик, двойные",
                    "Дуб, двойные",
                    "Одинарный бар"
                ],
                [
                    "Тик, кольцо",
                    "Дуб, кольцо",
                    "Вытянутый свет"
                ],
                [
                    "Тик, ниша",
                    "Дуб, ниша",
                    "Круглая ниша"
                ],
                [
                    "Тик, настенный",
                    "Дуб, настенный",
                    "Треугольный свет"
                ],
                [
                    "Тик, прямоугольный",
                    "Дуб, прямоугольный",
                    "Круглая подсветка"
                ],
                [
                    "Тик, одинарный",
                    "Дуб, одинарный",
                    "Двойной слой"
                ],
                [
                    "Тик, настольный",
                    "Дуб, настольный",
                    "Прямоугольный свет"
                ],
                [
                    "Тик, прямоугольный",
                    "Дуб, прямоугольный",
                    "Круглый контроллер"
                ]
            ],
            "materials": [
                [
                    "Тёплый дуб",
                    "Светлый бук",
                    "Окованная кромка"
                ],
                [
                    "Тёплый дуб",
                    "Светлый бук",
                    "Поднятая бордюр"
                ],
                [
                    "Натуральный травертин",
                    "Натуральный травертин",
                    "Текстурный рельеф"
                ],
                [
                    "Микрецем безшовный",
                    "Микрецем безшовный",
                    "Волнообразный узор"
                ],
                [
                    "Травертин крупный",
                    "Травертин крупный",
                    "Тонкая профиль"
                ],
                [
                    "Чёрный мат",
                    "Чёрный мат",
                    "Выдолбленный профиль"
                ],
                [
                    "Бежевый хлопок",
                    "Бежевый лен",
                    "Меньший квадрат"
                ],
                [
                    "Бежевый хлопок",
                    "Бежевый лен",
                    "Тёплый бордюр"
                ],
                [
                    "Чёрный каркас",
                    "Чёрный каркас",
                    "Выше и узко"
                ]
            ],
            "humor": [
                [
                    "Объёмный квази‑попугай",
                    "Колоссальный кофейный кружка",
                    "Гигантская шишка"
                ],
                [
                    "Кошачий кран",
                    "Кoi‑поток для кота",
                    "Кроличий бассейн"
                ],
                [
                    "Клинок‑раковина",
                    "Щит‑зеркало",
                    "Кипяток‑ванна"
                ],
                [
                    "Ковёр‑помощник",
                    "Радио‑потолок",
                    "Плакат‑полка"
                ],
                [
                    "Монеты‑труба",
                    "Медные монеты",
                    "Серебряный поток"
                ],
                [
                    "Качели‑ванна",
                    "Бочка‑вход",
                    "Конвейер‑полотенце"
                ],
                [
                    "Арбуз‑ванна",
                    "Ананас‑ванна",
                    "Апельсин‑ванна"
                ],
                [
                    "Кубик‑дозатор",
                    "Сода‑диспенсер",
                    "Вендин‑полка"
                ],
                [
                    "Буря‑сноркель",
                    "Кoi‑шляпа",
                    "Лебедь‑маска"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Асимметричные двери"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Высокий узкий профиль"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Удлинённый прямоугольник"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Тонкая вертикаль"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Низкая посадка"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Удлинённый прямоугольник"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Низкая посадка"
                ],
                [
                    "Тёмный дуб кран",
                    "Светлый дуб кран",
                    "Низкий профиль крана"
                ],
                [
                    "Светлый дуб, зерно",
                    "Темный дымчатый дуб",
                    "Высокие узкие полки"
                ]
            ],
            "lighting": [
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Плоская панель"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Круглая коника"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Линейный бар"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Косые светильники"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Линейный рельс"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Плоская полоса"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Панельный свет"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Прямоугольный свет"
                ],
                [
                    "Тёмный дуб",
                    "Светлый дуб",
                    "Тонкая полоса"
                ]
            ],
            "materials": [
                [
                    "Светлый дуб, естественный",
                    "Темный дуб, дымчатый",
                    "Диагональный срез"
                ],
                [
                    "Бежевый травертин, глянцевый",
                    "Травертин, пористый",
                    "Утолщённый профиль"
                ],
                [
                    "Тёплый известковый, мягкий",
                    "Текстурный известковый",
                    "Прямоугольный плитка"
                ],
                [
                    "Светлый дуб, матовый",
                    "Темный дуб, дымчатый",
                    "Низкий фасад, светлый"
                ],
                [
                    "Тёплая известковая стена",
                    "Текстурный известковый",
                    "Вертикальная полоса"
                ],
                [
                    "Короткая, черный",
                    "Короткая, двойная",
                    "Удлинённая, черный"
                ],
                [
                    "Травертин, глянцевый",
                    "Травертин, матовый",
                    "Глубокая чашка"
                ],
                [
                    "Лён, грубая ткань",
                    "Хлопок, вафельный",
                    "Сложенный лён"
                ],
                [
                    "Лён, рулон",
                    "Хлопок, вафельный",
                    "Свернутый лён"
                ]
            ],
            "humor": [
                [
                    "Гигантская вилка",
                    "Колоссальная ложка",
                    "Монументальный черпак"
                ],
                [
                    "Кот‑шеф мини",
                    "Кролик‑пекарь",
                    "Хомяк‑кухня"
                ],
                [
                    "Каст‑ирон котел‑плита",
                    "Каменный ступка‑раковина",
                    "Бочка‑холодильник"
                ],
                [
                    "ЗИЛ‑холодильник с ковриком",
                    "Самовар на острове",
                    "Металлический стол‑карусель"
                ],
                [
                    "Толпа мини‑шефов",
                    "Сотни ложек‑поваров",
                    "Гора досок‑поваров"
                ],
                [
                    "Гамаки вместо стульев",
                    "Качели над кухней",
                    "Низкая скамья‑отдых"
                ],
                [
                    "Остров‑пончик",
                    "Остров‑крендель",
                    "Остров‑круассан"
                ],
                [
                    "Пинбол в острове",
                    "Клешня‑аркада",
                    "Джукбокс‑интеграция"
                ],
                [
                    "Гном‑шеф неоновый",
                    "Робот‑повар неоновый",
                    "Суши‑статуя неоновая"
                ]
            ]
        }
    },
    "modern_classic": {
        "living": {
            "furniture": [
                [
                    "Вельвет, классический",
                    "Шёлк, элегантный",
                    "Низко‑седой, чаяк"
                ],
                [
                    "Вельвет, классический",
                    "Шёлк, элегантный",
                    "Крылатый, округлый"
                ],
                [
                    "Мрамор, прямоугольный",
                    "Мрамор, дубовый",
                    "Круглый, закруглённый"
                ],
                [
                    "Великий, квадратный",
                    "Дубовый, квадратный",
                    "Округлый, квадратный"
                ],
                [
                    "Мрамор, низкий",
                    "Мрамор, низкий",
                    "Колонный, высокий"
                ],
                [
                    "Мрамор, прямой",
                    "Мрамор, прямой",
                    "Лестничный, конический"
                ],
                [
                    "Вельвет, классический",
                    "Шёлк, элегантный",
                    "Высокий, узкий"
                ],
                [
                    "Мрамор, прямой",
                    "Мрамор, прямой",
                    "Овальный, округлый"
                ],
                [
                    "Вельвет, классический",
                    "Шёлк, элегантный",
                    "Прямоугольный, низкий"
                ]
            ],
            "lighting": [
                [
                    "Античный латунный",
                    "Шампань бронза",
                    "Круглый латунный"
                ],
                [
                    "Античный латунный узор",
                    "Шампань бронза узор",
                    "Двойные кабельные"
                ],
                [
                    "Античный латунный рукоятка",
                    "Шампань бронза рукоятка",
                    "Круглые ножки"
                ],
                [
                    "Античный латунный корпус",
                    "Шампань бронза корпус",
                    "Геометрический корпус"
                ],
                [
                    "Античный латунный рукоятка",
                    "Шампань бронза рукоятка",
                    "Двойные рукоятки"
                ],
                [
                    "Античный латунный круг",
                    "Шампань бронза круг",
                    "Античный латунный квадрат"
                ],
                [
                    "Античный латунный канал",
                    "Шампань бронза канал",
                    "Античный латунный профиль"
                ],
                [
                    "Античный латунный бар",
                    "Шампань бронза бар",
                    "Поворотный рычаг"
                ],
                [
                    "Античный латунный панель",
                    "Шампань бронза панель",
                    "Античный латунный прямоугольник"
                ]
            ],
            "materials": [
                [
                    "Тёмный орех",
                    "Эбонированный дуб",
                    "Прямоугольный слэб"
                ],
                [
                    "Светло‑серый гипс",
                    "Гладкая поверхность",
                    "Королевская молдинг"
                ],
                [
                    "Симметричный фасет",
                    "Гладкая поверхность",
                    "Вертикальная молдинг"
                ],
                [
                    "Белый потолок",
                    "Элегантный молдинг",
                    "Прямоугольный блок"
                ],
                [
                    "Изумрудный бархат",
                    "Слоновая шелк",
                    "Прямоугольный ковер"
                ],
                [
                    "Изумрудный бархат",
                    "Слоновая шелк",
                    "Вертикальная молдинг"
                ],
                [
                    "Изумрудный бархат",
                    "Слоновая шелк",
                    "Удлинённый прямоугольник"
                ],
                [
                    "Изумрудный бархат",
                    "Слоновая шелк",
                    "Квадрат с фаской"
                ],
                [
                    "Античный латунный",
                    "Шампань бронза",
                    "Прямой брусок"
                ]
            ],
            "humor": [
                [
                    "Гранитный камень",
                    "Бронзовый чайник",
                    "Деревянный шкаф"
                ],
                [
                    "Кошачий трон",
                    "Лапы‑браслет",
                    "Кот‑кровать"
                ],
                [
                    "Самурай‑подставка",
                    "Щит‑стол",
                    "Статуя‑лампа"
                ],
                [
                    "Олень‑ковёр",
                    "Самовар‑стол",
                    "Плакат‑витрина"
                ],
                [
                    "Мраморный слон",
                    "Гигантская жирафа",
                    "Брюшко кита"
                ],
                [
                    "Гамак‑колонна",
                    "Парящий стеклянный остров",
                    "Скользящий качель"
                ],
                [
                    "Кресло‑авокадо",
                    "Лежак‑пицца",
                    "Софа‑суши"
                ],
                [
                    "Клешня‑аркада",
                    "Джукбокс‑буфет",
                    "Пинбол‑стол"
                ],
                [
                    "Будда в VR",
                    "Будда в неоне",
                    "Будда с книгами"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Глянцевый орех, латунь",
                    "Эбони, розовое золото",
                    "Платформенный, скрытая латунь"
                ],
                [
                    "Шёлк‑хлопок, латунные детали",
                    "Эбони, розовое золото",
                    "Крылья, орех, латунные"
                ],
                [
                    "Орех, мраморный акцент",
                    "Эбони, розовое золото, бархат",
                    "Парящий, мраморная плита"
                ],
                [
                    "Орех, бархатный вкладыш",
                    "Эбони, розовое золото, шёлк",
                    "Вертикальный, мраморный верх"
                ],
                [
                    "Орех, шелковая подкладка",
                    "Эбони, розовое золото, бархат",
                    "Бифолд, мраморная полоска"
                ],
                [
                    "Орех, бархатная подушка",
                    "Эбони, розовое золото, шёлк",
                    "Изогнутый, мраморная поверхность"
                ],
                [
                    "Орех, мрамор, шёлк",
                    "Эбони, розовое золото, овальная форма",
                    "Круглая, латунные ножки, шёлк"
                ],
                [
                    "Орех, шампанское шелк, латунь",
                    "Эбони, розовое золото, бархат",
                    "Крыло‑спинка, орех, латунные"
                ],
                [
                    "Орех, латунная окантовка",
                    "Эбони, розовое золото, овал",
                    "Арка, мраморный вставка"
                ]
            ],
            "lighting": [
                [
                    "Бронзовая отделка, розовое стекло",
                    "Тёмный дуб, бордовый оттенок",
                    "Линейный бар, минимализм"
                ],
                [
                    "Бронзовый держатель, шелковый розовый",
                    "Тёмный дуб, бархатный бордовый",
                    "Три диска, розовый кристалл"
                ],
                [
                    "Ореховый осн., шампанский шелк",
                    "Тёмный дуб, бархатный бордовый",
                    "Трипод, розовое стекло"
                ],
                [
                    "Орех, янтарное стекло, розовый",
                    "Тёмный дуб, тканевая тень",
                    "Изогнутая рука, акриловый панель"
                ],
                [
                    "Бронзовый обод, розовое стекло",
                    "Тёмный дуб, бордовый тонир",
                    "Линейный свет, розовый диффузор"
                ],
                [
                    "Бронзовое кольцо, розовый свет",
                    "Тёмный дуб, бордовый свет",
                    "Плавающая панель, розовый ореол"
                ],
                [
                    "Бронзовый корпус, розовый диффузор",
                    "Тёмный дуб, бордовый тонир",
                    "Линейный свет, розовый стекло"
                ],
                [
                    "Бронзовый осн., розовый кант",
                    "Тёмный дуб, бордовый бархат",
                    "Трипод, розовая трубка"
                ],
                [
                    "Бронзовое кольцо, розовый свет",
                    "Тёмный дуб, бордовый свет",
                    "Распашка, розовый панель"
                ]
            ],
            "materials": [
                [
                    "Бордовый орех с розой",
                    "Эбони с золотой полосой",
                    "Диагональный орех, розовый край"
                ],
                [
                    "Теплый тауп с латунью",
                    "Эбони с розовым золотом",
                    "Разделённый орех‑тауп"
                ],
                [
                    "Тауапный панельный штрих",
                    "Эбони с бархатным вкраплением",
                    "Треугольный орех‑мрамор"
                ],
                [
                    "Слоновая гипс с латунью",
                    "Эбони с розовым бархатом",
                    "Диагональный контраст"
                ],
                [
                    "Шампань шелк с розой",
                    "Бархатный мшистый с золотом",
                    "Пирамида с латунной завязкой"
                ],
                [
                    "Шампань с бархатным бордом",
                    "Эбони с золотой каймой",
                    "Круглая радиальная роза"
                ],
                [
                    "Шампань с бархатными завязками",
                    "Эбони с золотыми петлями",
                    "Полу‑широкая розовая кромка"
                ],
                [
                    "Бархат мшистый с шелковой окантовкой",
                    "Эбони с золотыми кольцами",
                    "Диагональная двойная ткань"
                ],
                [
                    "Латунный ручка с розовой эмалью",
                    "Розовое золото с бархатным врезом",
                    "Латунный шарнир с розовым пунктом"
                ]
            ],
            "humor": [
                [
                    "Ремешок как изголовье",
                    "Ремешок вместо тумбы",
                    "Коронка как консоль"
                ],
                [
                    "Кровать для собаки",
                    "Будка с кроватью",
                    "Лежанка с тумбой"
                ],
                [
                    "Доспех как подставка",
                    "Шлем на тумбе",
                    "Ножны как тумба"
                ],
                [
                    "Ковер и стенка",
                    "Диван Победа",
                    "Кресло на стене"
                ],
                [
                    "Бревно как кровать",
                    "Берёзовый диван",
                    "Резное изголовье"
                ],
                [
                    "Яма с шарами",
                    "Песочница в полу",
                    "Песочница с лавочкой"
                ],
                [
                    "Желток как подушка",
                    "Блины с сиропом",
                    "Омлет матрасом"
                ],
                [
                    "Диско-шар на потолке",
                    "Вращающаяся люстра",
                    "Хрустальный шар"
                ],
                [
                    "Олень с неоном",
                    "Львиная грива",
                    "Лось в изножье"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Ореховое дерево",
                    "Тёмный дуб",
                    "Настенная модель"
                ],
                [
                    "Ореховая окантовка",
                    "Тёмный дуб",
                    "Овальная форма"
                ],
                [
                    "Ореховые панели",
                    "Тёмный дуб",
                    "Прямоугольная форма"
                ],
                [
                    "Ореховая рама",
                    "Тёмный дуб",
                    "Криволинейная форма"
                ],
                [
                    "Ореховая крышка",
                    "Тёмный дуб",
                    "Настенный монтаж"
                ],
                [
                    "Ореховые двери",
                    "Тёмный дуб",
                    "Низкий профиль"
                ],
                [
                    "Ореховые боковины",
                    "Тёмный дуз",
                    "Высокий узкий"
                ],
                [
                    "Ореховое сиденье",
                    "Тёмный дуб",
                    "Изогнутая форма"
                ],
                [
                    "Классическая лестница",
                    "Тёмный дуб",
                    "Двойная колонна"
                ]
            ],
            "lighting": [
                [
                    "Латунный корпус, мрамор",
                    "Эбонированный дуб, шампан",
                    "Круглая форма, купол"
                ],
                [
                    "Бра с орехом, эмаль",
                    "Эбонированный дуб, шампан",
                    "Одинарный бар, кристалл"
                ],
                [
                    "Три кана, орех",
                    "Три кана, дуб",
                    "Четыре линейных канала"
                ],
                [
                    "Кармашек с орехом",
                    "Кармашек с дубом",
                    "Продолговатый светильник"
                ],
                [
                    "Бра с орехом, шампан",
                    "Бра с дубом, шампан",
                    "Бра‑бар, орех"
                ],
                [
                    "Зеркало с орехом, эмаль",
                    "Зеркало с дубом, эмаль",
                    "Круглое зеркало, орех"
                ],
                [
                    "Непрерывный свет, орех",
                    "Непрерывный свет, дуб",
                    "Сегментный свет, орех"
                ],
                [
                    "Торшер с орехом, сатин",
                    "Торшер с дубом, сатин",
                    "Сфера, орех"
                ],
                [
                    "Панель с орехом, индикатор",
                    "Панель с дубом, индикатор",
                    "Круглый диал, орех"
                ]
            ],
            "materials": [
                [
                    "Золотой мрамор квадрат",
                    "Эбонированный дуб квадрат",
                    "Шестиугольный мрамор"
                ],
                [
                    "Мрамор прямоугольный",
                    "Дуб прямоугольный",
                    "Вертикальный мрамор"
                ],
                [
                    "Мраморные панели",
                    "Дубовые панели",
                    "Комбинированные полосы"
                ],
                [
                    "Латунная отделка",
                    "Шампань-руль",
                    "Ступенчатый карниз"
                ],
                [
                    "Мраморная столешница",
                    "Дубовая столешница",
                    "Утолщенный мрамор"
                ],
                [
                    "Латунный кран с золотом",
                    "Дубовая ручка",
                    "Двойные ручки"
                ],
                [
                    "Вышивка изумруд",
                    "Сатиновый край",
                    "Шёлковая лента"
                ],
                [
                    "Квадратный коврик",
                    "Квадрат с сатином",
                    "Круглый коврик"
                ],
                [
                    "Латунная рама",
                    "Дубовая рама",
                    "Изогнутый профиль"
                ]
            ],
            "humor": [
                [
                    "Золотой утка‑ванна",
                    "Утка‑трон из мрамора",
                    "Утка‑плавающая в чаше"
                ],
                [
                    "Кошачьи лапы‑ножки",
                    "Кошачий кран‑лапы",
                    "Ухо‑ванна с усами"
                ],
                [
                    "Шлем‑раковина рыцаря",
                    "Шлем‑раковина ренессанс",
                    "Викинг‑шлем в раковине"
                ],
                [
                    "Медаль‑мыльница",
                    "Молот‑сокрушитель мыла",
                    "Звезда‑мыльница"
                ],
                [
                    "Монеты в сливе",
                    "Водопад монет",
                    "Спираль монет"
                ],
                [
                    "Горка‑ванна",
                    "Горка‑клавфут",
                    "Горка‑бассейн"
                ],
                [
                    "Арбуз‑ванна",
                    "Ананас‑ванна",
                    "Персик‑ванна"
                ],
                [
                    "Ванна‑жевательная машина",
                    "Ванна‑торговый автомат",
                    "Ванна‑дозатор газировки"
                ],
                [
                    "Бюст‑маска с неоном",
                    "Бюст‑маска‑сияние",
                    "Бюст‑сноркель‑ореол"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Кедровый фасад",
                    "Эбонитовый стиль",
                    "Компактный корпус"
                ],
                [
                    "Кедровый фасад",
                    "Эбонитовый стиль",
                    "Тонкая панель"
                ],
                [
                    "Кедровый остров",
                    "Эбонитовый базис",
                    "Тонкая форма"
                ],
                [
                    "Кедровый столб",
                    "Эбонитовый профиль",
                    "Компактный столб"
                ],
                [
                    "Кедровые ножки",
                    "Эбонитовый корпус",
                    "Низкая посадка"
                ],
                [
                    "Кедровый столб",
                    "Эбонитовый корпус",
                    "Круглый стол"
                ],
                [
                    "Кедровый спин",
                    "Эбонитовый корпус",
                    "Высокий спин"
                ],
                [
                    "Кедровый гарнитур",
                    "Эбонитовый ванна",
                    "Минимализм"
                ],
                [
                    "Кедровые полки",
                    "Эбонитовый дизайн",
                    "Тонкая полка"
                ]
            ],
            "lighting": [
                [
                    "Бронзовое кольцо, кремовый карниз",
                    "Бронза и дуб, островная база",
                    "Сетка квадратов, дубовая окантовка"
                ],
                [
                    "Бронзовая клетка, бордовый стекло",
                    "Шампанская рама, зелёный керамик",
                    "Три мини‑клетки, бордовые шары"
                ],
                [
                    "Бронзовый канал, ореховые шкафы",
                    "Шампанный профиль, дубовые шкафы",
                    "Три баров, ореховые концы"
                ],
                [
                    "Бронзовые кольца, кремовый гипс",
                    "Шампанный трим, дубовые рамы",
                    "Круглая сетка, бронзовый диффузор"
                ],
                [
                    "Бронзовый трек, ореховый кросс",
                    "Шампанный трек, дубовый рельс",
                    "Два пересекающихся трека, орех"
                ],
                [
                    "Бронзовая полоска, ореховый цоколь",
                    "Шампанный LED, дубовый цоколь",
                    "Квадратный корпус, ореховый"
                ],
                [
                    "Бронзовый спот, ореховая полка",
                    "Шампанный свет, дубовая панель",
                    "Два спота, ореховые полки"
                ],
                [
                    "Бронзовая рама, бордовый шёлк",
                    "Шампанный каркас, зелёный шёлк",
                    "Парные мини‑клетки, бордовые шары"
                ],
                [
                    "Бронзовая панель, ореховые кнопки",
                    "Шампанный корпус, дубовые кнопки",
                    "Круглая ручка, бронзовый кольцо"
                ]
            ],
            "materials": [
                [
                    "Квадратные панели",
                    "Темный матовый прямоугольник",
                    "Елочка под углом"
                ],
                [
                    "Классический прямоугольник",
                    "Бронзовый акцент",
                    "Круглый остров"
                ],
                [
                    "Бордовый акцент",
                    "Бронзовая вертикаль",
                    "Шестигранные плитки"
                ],
                [
                    "Поднятые панели",
                    "Гладкие тёмные",
                    "Изогнутый полукруг"
                ],
                [
                    "Кремовый молдинг",
                    "Бронзовый молдинг",
                    "Ребристый с латунью"
                ],
                [
                    "Изогнутые пуговицы",
                    "Прямоугольные с бронзой",
                    "Спиральный дизайн"
                ],
                [
                    "Прямой чаша",
                    "Прямоугольный с бронзой",
                    "Овальная чаша"
                ],
                [
                    "Лён с вышивкой",
                    "Шёлк с льном",
                    "Волнистый силуэт"
                ],
                [
                    "Лён с полосой",
                    "Шёлк с льном",
                    "Складчатый дизайн"
                ]
            ],
            "humor": [
                [
                    "Брутальный латунный вилочный монстр",
                    "Мраморная гигантская ложка",
                    "Подвешенный серебряный черпак"
                ],
                [
                    "Котячий мини-рабочий уголок",
                    "Кроличий кухонный ниши",
                    "Хомячий крошечный стенд"
                ],
                [
                    "Средневековый чугунный котел",
                    "Каменный ступа‑пестик",
                    "Кузнечный железный колокол"
                ],
                [
                    "Алюминиевый ЗИЛ‑холодильник",
                    "Металлическая советская кладовая",
                    "Стальная столовая с самоваром"
                ],
                [
                    "Блестящие латунные шефы",
                    "Мини‑латунные лопатки",
                    "Латунные черпачки‑сторожи"
                ],
                [
                    "Брасс‑гамак над плитой",
                    "Металлическое качающее кресло",
                    "Шезлонг с латунными ножками"
                ],
                [
                    "Кольцевая пончиковая островка",
                    "Круассан‑контурный остров",
                    "Тортовый кусок‑остров"
                ],
                [
                    "Пинбол‑остров в стиле",
                    "Аркадный шкаф‑плита",
                    "Джукбокс‑стенка кухни"
                ],
                [
                    "Гном‑шеф с неоновыми глазами",
                    "Курочка‑потрошитель с неоном",
                    "Дракон‑скульптура с неоном"
                ]
            ]
        }
    },
    "scandi": {
        "living": {
            "furniture": [
                [
                    "Материал: брус",
                    "Текстура: овца",
                    "Форма: chaise"
                ],
                [
                    "Форма: прямолинейный",
                    "Текстура: овца",
                    "Форма: округлый"
                ],
                [
                    "Топ: брус",
                    "Топ: лак",
                    "Профиль: низкий"
                ],
                [
                    "Форма: прямой",
                    "Топ: лак",
                    "Форма: круглый"
                ],
                [
                    "Панели: брус",
                    "Топ: лак",
                    "Плавающий"
                ],
                [
                    "Стелька: брус",
                    "Стелька: лак",
                    "Стиль: лестница"
                ],
                [
                    "Ручка: металл",
                    "Ручка: лак",
                    "Форма: высокий"
                ],
                [
                    "Топ: брус",
                    "Топ: лак",
                    "Форма: узкая"
                ],
                [
                    "База: брус",
                    "База: лак",
                    "Форма: круглая"
                ]
            ],
            "lighting": [
                [
                    "Светлая береза",
                    "Светлый ясень",
                    "Широкий низкий профиль"
                ],
                [
                    "Березовый абажур",
                    "Ясеневый абажур",
                    "Три ветви"
                ],
                [
                    "Березовая тренога",
                    "Ясень с овечьей шкурой",
                    "Одинарная нога"
                ],
                [
                    "Березовое основание",
                    "Ясень и овечья шкура",
                    "Цилиндрическая форма"
                ],
                [
                    "Березовый корпус",
                    "Ясеневый корпус",
                    "Треугольная форма"
                ],
                [
                    "Березовая окантовка",
                    "Ясеневая окантовка",
                    "Линейная схема"
                ],
                [
                    "Березовый канал",
                    "Ясеневый канал",
                    "Ступенчатый выступ"
                ],
                [
                    "Березовая рамка",
                    "Ясеневая рамка",
                    "Двойные ветви"
                ],
                [
                    "Березовая панель",
                    "Ясеневая панель",
                    "Круглая форма"
                ]
            ],
            "materials": [
                [
                    "Биржа с розой",
                    "Ясень с розой",
                    "Биржа с кромкой"
                ],
                [
                    "Белый матовый",
                    "Гладкая плита",
                    "Рельефная панель"
                ],
                [
                    "Геометрия роза",
                    "Тот же узор",
                    "Вертикальная полоса"
                ],
                [
                    "Белый с линией",
                    "Гладкий потолок",
                    "Панельный потолок"
                ],
                [
                    "Классический плоский",
                    "Слой овчины",
                    "Круглый ковер"
                ],
                [
                    "Прямые складки",
                    "Слой овчины",
                    "Вертикальный плет"
                ],
                [
                    "Базовый жаккард",
                    "Слой овчины",
                    "Диагональная текстура"
                ],
                [
                    "Классический набор",
                    "Деревянные чехлы",
                    "Круглые подушки"
                ],
                [
                    "Тонкая ручка",
                    "Тот же стиль",
                    "Изогнутая ручка"
                ]
            ],
            "humor": [
                [
                    "Гладкий камень",
                    "Книга‑кресло",
                    "Гигантский пульт"
                ],
                [
                    "Деревянный котовый трон",
                    "Черный стальной кот",
                    "Котий замок"
                ],
                [
                    "Самурай‑подставка",
                    "Викинг‑щит",
                    "Греческая колонна"
                ],
                [
                    "Олень на стене",
                    "Плакат‑пропаганда",
                    "Кукольный шкаф"
                ],
                [
                    "Слон в комнате",
                    "Лис в курятнике",
                    "Жираф в облаках"
                ],
                [
                    "Гармоничная гамак",
                    "Стальная качеля",
                    "Подвесной кокон"
                ],
                [
                    "Авокадо‑кресло",
                    "Лимонный стул",
                    "Суши‑стол"
                ],
                [
                    "Клав‑машина",
                    "Пинбол‑консоль",
                    "Джукбокс‑полка"
                ],
                [
                    "Будда в VR",
                    "Блок‑робот",
                    "Скамья‑гарнитура"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Подвесная кровать"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Парящий изголовье"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Парящая тумба"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Высокий комод"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Узкий шкаф"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Низкая скамья"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Парящий стол"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Низкое кресло"
                ],
                [
                    "Белый берёзовый",
                    "Бледный сосновый",
                    "Парящее зеркало"
                ]
            ],
            "lighting": [
                [
                    "Берёзовый корпус",
                    "Сосновый корпус",
                    "Удлинённый профиль"
                ],
                [
                    "Берёзовый держатель",
                    "Сосновый держатель",
                    "Узкая вытянутая форма"
                ],
                [
                    "Берёзовый подиум",
                    "Сосновый подиум",
                    "Компактный стройный"
                ],
                [
                    "Берёзовая планка",
                    "Сосновая планка",
                    "Вертикальный силуэт"
                ],
                [
                    "Берёзовый корпус",
                    "Сосновый корпус",
                    "Тонкий профиль"
                ],
                [
                    "Берёзовый монтаж",
                    "Сосновый монтаж",
                    "Удлинённый дизайн"
                ],
                [
                    "Берёзовый отсек",
                    "Сосновый отсек",
                    "Компактный дизайн"
                ],
                [
                    "Берёзовый подиум",
                    "Сосновый подиум",
                    "Высокий силуэт"
                ],
                [
                    "Берёзовый корпус",
                    "Сосновый корпус",
                    "Низкий профиль"
                ]
            ],
            "materials": [
                [
                    "Брус с берёзой",
                    "Светлая сосна",
                    "Диагональная стружка"
                ],
                [
                    "Матовая белизна",
                    "Сосновый шпон",
                    "Тонкая линия"
                ],
                [
                    "Панель с пятном",
                    "Вертикальная полоса",
                    "Ступенчатый профиль"
                ],
                [
                    "Белый гипс",
                    "Сосновые планки",
                    "Круглая вставка"
                ],
                [
                    "Чистый хлопок",
                    "Сосновый оттенок",
                    "Сложенный слой"
                ],
                [
                    "Толстый трикотаж",
                    "Шерстяная кожа",
                    "Круглая текстура"
                ],
                [
                    "Белый плотный",
                    "Сосновый оттенок",
                    "Сложенный образец"
                ],
                [
                    "Гладкая белизна",
                    "Тёплый трикотаж",
                    "Обводка кант"
                ],
                [
                    "Ручка с инкрустацией",
                    "Петля с акцентом",
                    "Тянущийся профиль"
                ]
            ],
            "humor": [
                [
                    "Наручные часы кроватью",
                    "Кухонный таймер диваном",
                    "Будильник футоном"
                ],
                [
                    "Кровать для собаки",
                    "Кошачий комплекс",
                    "Башня для хомяка"
                ],
                [
                    "Рыцарь у тумбы",
                    "Перо как лампа",
                    "Башня тумбочкой"
                ],
                [
                    "Ковёр на изголовье",
                    "Красный флаг ковром",
                    "Подушка с серпом"
                ],
                [
                    "Бревно кроватью",
                    "Ящик тумбочкой",
                    "Бочка ночником"
                ],
                [
                    "Шарики вместо кровати",
                    "Бассейн вместо дивана",
                    "Мяч вместо кресла"
                ],
                [
                    "Яичница кроватью",
                    "Пончик диваном",
                    "Бутерброд стулом"
                ],
                [
                    "Диско-шар люстрой",
                    "Люстра-снежинка",
                    "Лампа-звезда"
                ],
                [
                    "Олень с неоном",
                    "Сова на подсветке",
                    "Кролик LED ночник"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Бирчевые рейки, белый мрамор",
                    "Ясеневые рейки, кремовый коврик",
                    "Подвесные ножки, черные опоры"
                ],
                [
                    "Бирчевые рейки, черный кран",
                    "Ясеневые рейки, кремовый коврик",
                    "Настенный, бирчевые рейки"
                ],
                [
                    "Бирчевые ножки, свеча",
                    "Ясеневые ножки, льняной коврик",
                    "Тонкая форма, растение"
                ],
                [
                    "Бирчевые ниши, мыло",
                    "Ясеневые ниши, льняной коврик",
                    "Криволинейное стекло, растение"
                ],
                [
                    "Бирчевые крышка, плитка",
                    "Ясеневые крышка, льняной коврик",
                    "Настенный, растение"
                ],
                [
                    "Бирчевые двери, полотенце",
                    "Ясеневые двери, льняной коврик",
                    "Широкое основание, растение"
                ],
                [
                    "Бирчевые рамы, полотенце",
                    "Ясеневые рамы, льняной коврик",
                    "Скруглённый верх, растение"
                ],
                [
                    "Бирчевые ножки, подушка",
                    "Ясеневые ножки, льняная подушка",
                    "Изогнутый сид, подушка"
                ],
                [
                    "Бирчевые планки, растение",
                    "Ясеневые планки, льняной коврик",
                    "Широкие планки, свернутый полотенце"
                ]
            ],
            "lighting": [
                [
                    "Круглая берёзовая решётка",
                    "Прямоугольный ясеневый диффузор",
                    "Узкая линейная планка"
                ],
                [
                    "Двойные берёзовые абажуры",
                    "Двойные ясеневые абажуры",
                    "Одинокий берёзовый овал"
                ],
                [
                    "Берёзовые внутренние жалюзи",
                    "Ясеневые внутренние жалюзи",
                    "Удлинённый берёзовый профиль"
                ],
                [
                    "Берёзовая решётка ниши",
                    "Ясеневый решётка ниши",
                    "Круглый берёзовый ореол"
                ],
                [
                    "Берёзовый абажур бра",
                    "Ясеневый абажур бра",
                    "Линейный берёзовый бар"
                ],
                [
                    "Берёзовая задняя панель",
                    "Ясенеевая задняя панель",
                    "Овальная берёзовая рамка"
                ],
                [
                    "Берёзовый вентиляционный слот",
                    "Ясеневый вентиляционный слот",
                    "Реконфигурированный берёзовый профиль"
                ],
                [
                    "Берёзовый верхний диффузор",
                    "Ясеневый верхний диффузор",
                    "Берёзовое кольцо‑свет"
                ],
                [
                    "Берёзовое тактильное кольцо",
                    "Ясеневое тактильное кольцо",
                    "Берёзовый прямоугольный край"
                ]
            ],
            "materials": [
                [
                    "Белый матовый",
                    "С серой рамкой",
                    "Диагональная форма"
                ],
                [
                    "Белый классический",
                    "С серой рамкой",
                    "Винтажный узор"
                ],
                [
                    "Плоские панели",
                    "С серой рамкой",
                    "Саженский акцент"
                ],
                [
                    "Плоский потолок",
                    "С серой полосой",
                    "Саженский акцент"
                ],
                [
                    "Чистый край",
                    "С серой окантовкой",
                    "Саженский канал"
                ],
                [
                    "Чёрный лак",
                    "С серой ручкой",
                    "Длинный горлышко"
                ],
                [
                    "Белый хлопок",
                    "С кремовой оберткой",
                    "Саженский шов"
                ],
                [
                    "Кремовый текстур",
                    "С серой подложкой",
                    "Круглый край"
                ],
                [
                    "Чистый стекло",
                    "С серыми полосами",
                    "Саженский окрас"
                ]
            ],
            "humor": [
                [
                    "Жёлтая утка‑ванна",
                    "Деревянная ложка‑крючок",
                    "Чайный кубок‑басейн"
                ],
                [
                    "Кошачьи уши‑полотенца",
                    "Хомячьи лестницы‑полки",
                    "Птичий фонтан‑зеркало"
                ],
                [
                    "Шлем‑раковина",
                    "Телескоп‑вешалка",
                    "Штурвал‑хранитель"
                ],
                [
                    "Совхозный ковер‑декор",
                    "Весы‑табурет",
                    "Фигурка‑мыльница"
                ],
                [
                    "Монетный вихрь",
                    "Деньги‑полотенца",
                    "Кард‑дозатор"
                ],
                [
                    "Горка‑в‑ванну",
                    "Полицейский столб‑полотенце",
                    "Труба‑подогрев"
                ],
                [
                    "Арбузный бассейн",
                    "Пончик‑ванна",
                    "Авокадо‑раковина"
                ],
                [
                    "Жвачка‑дозатор",
                    "Попкорн‑подогрев",
                    "Автомат‑туалетная бумага"
                ],
                [
                    "Бюст‑сноркель",
                    "Статуя‑смартфон",
                    "Картина‑аквариум"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Белый берёзовый фасад",
                    "Ясень с берёзовым краем",
                    "Компактный низкий блок"
                ],
                [
                    "Белый берёзовый шкаф",
                    "Ясень с берёзовым краем",
                    "Узкий высокий стэк"
                ],
                [
                    "Белый берёзовый остров",
                    "Ясень с берёзовым краем",
                    "Низкая плоская столешница"
                ],
                [
                    "Белый берёзовый пенал",
                    "Ясень с берёзовым краем",
                    "Интегрированные полки"
                ],
                [
                    "Белый берёзовый сиденье",
                    "Ясень с берёзовым краем",
                    "Таперные сиденья"
                ],
                [
                    "Белый берёзовый стол",
                    "Ясень с берёзовым краем",
                    "Овальный стол"
                ],
                [
                    "Белый берёзовый стул",
                    "Ясень с берёзовым краем",
                    "Таперные спинки"
                ],
                [
                    "Белый берёзовый край",
                    "Ясень с берёзовым краем",
                    "Интегрированный блок"
                ],
                [
                    "Белый берёзовый полка",
                    "Ясень с берёзовым краем",
                    "Лестничный стеллаж"
                ]
            ],
            "lighting": [
                [
                    "Белый берёзовый",
                    "Пепельный ясен",
                    "Плоский встраиваемый"
                ],
                [
                    "Берёзовый с никелем",
                    "Ясеневый с никелем",
                    "Удлинённый профиль"
                ],
                [
                    "Берёзовый светодиод",
                    "Ясеневый светодиод",
                    "Компактный профиль"
                ],
                [
                    "Берёзовый софт‑линза",
                    "Ясеневый софт‑линза",
                    "Низкий профиль"
                ],
                [
                    "Берёзовый трек",
                    "Ясеневый трек",
                    "Короткий трек"
                ],
                [
                    "Берёзовый цоколь",
                    "Ясеневый цоколь",
                    "Высокий цоколь"
                ],
                [
                    "Берёзовый внутри",
                    "Ясеневый внутри",
                    "Узкий корпус"
                ],
                [
                    "Берёзовый подвес",
                    "Ясеневый подвес",
                    "Драматичный силуэт"
                ],
                [
                    "Берёзовый диммер",
                    "Ясеневый диммер",
                    "Узкий диммер"
                ]
            ],
            "materials": [
                [
                    "Брус с керамикой",
                    "Светлый ясень",
                    "Прямоугольный профиль"
                ],
                [
                    "Кварц с керамикой",
                    "Чистый кварц",
                    "Прямоугольный кусок"
                ],
                [
                    "Кафель с линиями",
                    "Классический плитка",
                    "Прямоугольный плит"
                ],
                [
                    "Брус с черной фурнитурой",
                    "Ясень с черной фурнитурой",
                    "Прямоугольный фасад"
                ],
                [
                    "Гладкая штукатурка",
                    "Однородный гипс",
                    "Прямоугольный образец"
                ],
                [
                    "Матовый черный",
                    "Тонкая черная",
                    "Прямоугольный держатель"
                ],
                [
                    "Никелевый полированный",
                    "Бесшовный никель",
                    "Прямоугольный образец"
                ],
                [
                    "Лён с серой полосой",
                    "Ткань с полосой",
                    "Прямоугольная ролька"
                ],
                [
                    "Лён с полосой",
                    "Шерстный коврик",
                    "Прямоугольный шерстяной"
                ]
            ],
            "humor": [
                [
                    "Колоссальный вилочный стол",
                    "Гигантская ложка",
                    "Божественно большой нож"
                ],
                [
                    "Котячий кухонный уголок",
                    "Кроличья кулинария",
                    "Хомячий барабан"
                ],
                [
                    "Кастрюля‑печь",
                    "Римский жаровник",
                    "Викинг‑печка"
                ],
                [
                    "ЗИЛ‑шкаф",
                    "Сталинский штамп",
                    "Красный кувшин"
                ],
                [
                    "Слишком много поваров",
                    "Много печенек",
                    "Переполненный специй"
                ],
                [
                    "Гамаки вместо стульев",
                    "Скачок на баре",
                    "Плавающая подушка"
                ],
                [
                    "Донат‑стол",
                    "Круассан‑площадка",
                    "Суши‑ролл остров"
                ],
                [
                    "Пинбол‑прибор",
                    "Аркадный шкаф",
                    "Футзал‑стол"
                ],
                [
                    "Гном‑повар",
                    "Гном‑бармен",
                    "Гном‑DJ"
                ]
            ]
        }
    }
}


NEG = ('people, person, human figure, ugly, deformed, noisy, blurry, low resolution, '
       'oversaturated, flat lighting, text, watermark, logo, clutter, dark')

def app_keyboard(editor=False, username=''):
    if editor:
        url = f'https://tp9mc.github.io?t={GH_PUBLISH_TOKEN}&u={username}'
    else:
        url = 'https://tp9mc.github.io'
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton(
        text='🏠 Открыть конструктор',
        web_app=telebot.types.WebAppInfo(url=url),
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
        # variant-aware lookup first; falls back to slot-only for legacy kitchen
        if f'{slot_num}_{variant}' in cat:
            return cat[f'{slot_num}_{variant}']
        if room == 'kitchen':
            return cat.get(str(slot_num), {})
        return cat.get(f'{slot_num}_{variant}', {})
    except (KeyError, TypeError):
        return {}


# Tail/boilerplate that must never reach the scene prompt (wastes the T5
# 256-token budget and, e.g. "white background", actively fights the scene).
_TAIL_RE = re.compile(
    r'\b(isolated on (a )?pure white background|isolated on white background|'
    r'on (a )?pure white background|white background|soft studio lighting|'
    r'studio lighting|ambient occlusion|centered front view|top-down view|'
    r'professional product photography|product photography|high quality|'
    r'photorealistic|photo realistic|hyper-detailed|highly detailed|'
    r'8k|4k|high resolution|crisp focus|commercial photography)\b',
    re.IGNORECASE)
_PREFIX_RE = re.compile(
    r'^\s*((minimalist\s+)?3d render of (an?|the)?\s*|'
    r'a (highly detailed|hyper-detailed)[^,]*render of (an?|the)?\s*|'
    r'positive\s*(prompt)?\s*\)?\s*:?\s*)', re.IGNORECASE)


def item_phrase(item: dict, max_segs: int = 2, max_chars: int = 75) -> str:
    """Compact English keyword phrase for the scene prompt.

    Aggressively strips render/quality boilerplate and the v2 tail so the
    limited T5 budget is spent on actual described objects, not on
    "white background, 8k, product photography" repeated 27 times.
    """
    pos = item.get('positive') or ''
    if not pos:
        return ''
    pos = _PREFIX_RE.sub('', pos)
    segs = []
    for raw in pos.split(','):
        s = _TAIL_RE.sub('', raw).strip(' .;')
        if not s or re.search(r'[а-яёА-ЯЁ]', s):
            continue
        # skip segments that became empty/junk after tail removal
        if len(s) < 3:
            continue
        segs.append(s)
        if len(segs) >= max_segs:
            break
    return ', '.join(segs)[:max_chars].strip(' ,')


def build_prompt_and_report(style, room, setup, catalog):
    """
    Returns (prompt_str, neg_str, selections_list).
    selections_list: list of (cat_id, n, variant, name_ru, phrase) for all 27 slots.
    """
    scene_parts = [STYLE_DNA[style], ROOM_DNA[room]]
    neg_parts   = [NEG]
    selections  = []  # (cat_id, n, variant, name_ru, phrase)

    # Collect selected items first; build the scene prompt under a token
    # budget afterwards (FLUX.1-schnell T5 truncates silently past ~256
    # tokens — empirically prompts beyond ~1.4k chars lose their tail, and
    # the tail is materials+lighting, so a full room quietly loses items).
    chosen = []  # (cat_id, n, variant, name_ru, full_phrase)
    for cat_id, px in CATS:
        cat_setup = setup.get(cat_id, {})
        for n in range(1, 10):
            slot_key = f'{px}_{n}'
            variant  = cat_setup.get(slot_key) or None
            if not variant:
                selections.append((cat_id, n, None, '', ''))
                continue
            item    = get_item(catalog, style, room, cat_id, n, variant)
            name_ru = item.get('name_ru', '')
            phrase  = item_phrase(item)
            chosen.append((cat_id, n, variant, name_ru, phrase, item))

    TAIL = ['professional interior photography', 'natural daylight',
            'wide angle lens', 'architectural digest', '8k']
    # Proven-safe budget: marker survived at 1411 chars, lost by 2146.
    # Cap the whole prompt at 1200 chars for margin.
    BUDGET = 1200
    fixed = ', '.join([STYLE_DNA[style], ROOM_DNA[room]] + TAIL)
    avail = BUDGET - len(fixed) - 4

    def assemble(phrases):
        body = ', '.join(p for p in phrases if p)
        return body, len(body)

    item_phrases = [c[4] for c in chosen]
    body, blen = assemble(item_phrases)
    degraded = False
    dropped = []
    if blen > avail:
        # step 1: shrink every item to its head noun (1 segment)
        degraded = True
        item_phrases = [item_phrase(c[5], max_segs=1, max_chars=42) for c in chosen]
        body, blen = assemble(item_phrases)
    if blen > avail:
        # step 2: drop items from the end (materials first — least scene
        # impact) until within budget; record what was dropped
        keep = list(zip(chosen, item_phrases))
        while keep and assemble([p for _, p in keep])[1] > avail:
            c, _ = keep.pop()
            dropped.append((c[0], c[1], c[2], c[3]))
        item_phrases = [p for _, p in keep]
        chosen_kept = {(c[0], c[1]) for c, _ in keep}
        body, blen = assemble(item_phrases)
    else:
        chosen_kept = {(c[0], c[1]) for c in chosen}

    for cat_id, n, variant, name_ru, phrase, item in chosen:
        selections.append((cat_id, n, variant, name_ru, phrase))
        item_neg = item.get('negative') or ''
        if item_neg:
            item_neg = re.sub(r'^[Nn]egative\s*[Pp]rompt\s*\)?\s*:?\s*', '', item_neg)
            item_neg = re.sub(r'^[Nn]egative\s*:?\s*', '', item_neg)
            clean_terms = [t.strip() for t in item_neg.split(',')
                           if t.strip() and not re.search(r'[а-яёА-ЯЁ]', t)]
            if clean_terms:
                neg_parts.append(', '.join(clean_terms))

    scene_parts = [STYLE_DNA[style], ROOM_DNA[room]]
    scene_parts += [p for p in item_phrases if p]
    scene_parts += TAIL
    prompt = ', '.join(p for p in scene_parts if p)
    build_prompt_and_report.last_budget = {
        'chars': len(prompt), 'items_selected': len(chosen),
        'items_kept': len(chosen) - len(dropped),
        'degraded': degraded, 'dropped': dropped,
    }
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

    # ── Бюджет промта (предупреждение об обрезке) ──────────────
    bud = getattr(build_prompt_and_report, 'last_budget', None)
    if bud:
        if bud['dropped']:
            lines += ['─' * W, '  ⚠ ВНИМАНИЕ: ПЕРЕПОЛНЕНИЕ ПРОМТА', '─' * W]
            lines.append(f'  Выбрано элементов: {bud["items_selected"]}')
            lines.append(f'  Учтено в генерации: {bud["items_kept"]}')
            lines.append(f'  Не поместилось:    {len(bud["dropped"])}')
            lines.append('  (модель FLUX ограничена ~256 токенами;')
            lines.append('   лишние элементы отброшены — выбери меньше')
            lines.append('   или сгенерируй по частям)')
            lines.append('')
        elif bud['degraded']:
            lines += ['─' * W, '  ℹ Промт сжат под лимит модели', '─' * W]
            lines.append(f'  Все {bud["items_selected"]} элементов учтены,')
            lines.append('  но описания укорочены (много выбрано).')
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
            vi       = {'main': 0, 'alt': 1, 'alt2': 2}.get(variant, 0)
            var_name = slot_opts[n - 1][vi] if (n - 1 < len(slot_opts) and vi < len(slot_opts[n - 1])) else ['A', 'Б', 'В'][vi]
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
        is_editor = message.chat.id in EDITOR_CHAT_IDS
        bot.send_message(
            message.chat.id,
            '👋 Привет! Нажми кнопку чтобы открыть конструктор.',
            reply_markup=app_keyboard(editor=is_editor, username=uname(message)),
        )

    @bot.message_handler(commands=['editor'])
    def on_editor(message):
        log_event(message.chat.id, uname(message), 'editor')
        if message.chat.id not in EDITOR_CHAT_IDS:
            bot.send_message(message.chat.id, '⛔ Только для редакторов.')
            return
        if not _tunnel_url:
            bot.send_message(message.chat.id,
                             '⏳ Туннель ещё поднимается, попробуй через 10–20 сек.')
            return
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(telebot.types.KeyboardButton(
            text='✏️ Открыть редактор',
            web_app=telebot.types.WebAppInfo(
                url=f'https://tp9mc.github.io?proxy={_tunnel_url}'
                    f'&t={GH_PUBLISH_TOKEN}&u=editor'),
        ))
        bot.send_message(
            message.chat.id,
            '✏️ Редактор готов. Кнопка ниже открывает Mini App с активным '
            'прокси — генерация по промту и публикация будут работать.',
            reply_markup=markup,
        )

    @bot.message_handler(commands=['stats'])
    def on_stats(message):
        log_event(message.chat.id, uname(message), 'stats')
        report = build_stats_report()
        buf = BytesIO(report.encode('utf-8'))
        buf.name = f'stats_{datetime.now().strftime("%Y%m%d_%H%M%S")}.txt'
        bot.send_document(message.chat.id, buf, caption='📊 Статистика бота')

    # ── Versions / rollback ─────────────────────────────────────────────
    REPO_DIR = os.path.dirname(os.path.abspath(__file__))

    def _git(*args, check=True) -> str:
        r = subprocess.run(['git', '-C', REPO_DIR, *args],
                           capture_output=True, text=True, timeout=60)
        if check and r.returncode != 0:
            raise RuntimeError(f'git {" ".join(args)}: {r.stderr.strip()}')
        return r.stdout.strip()

    def _git_log(n: int = 10) -> list:
        # tab-delimited hash, relative-date, ISO-date, subject
        out = _git('log', f'-n{n}', '--pretty=format:%h\t%cr\t%ci\t%s')
        items = []
        for line in out.splitlines():
            parts = line.split('\t', 3)
            if len(parts) == 4:
                items.append({'hash': parts[0], 'rel': parts[1],
                              'iso': parts[2][:16], 'subject': parts[3]})
        return items

    def _restart_bot_async():
        """Schedule self-restart in 1s so the current response can flush."""
        def _do():
            time.sleep(1)
            os.system(f'nohup /usr/local/bin/python3.14 "{__file__}" '
                      f'>> /tmp/bot.log 2>&1 &')
            os.kill(os.getpid(), signal.SIGTERM)
        threading.Thread(target=_do, daemon=True).start()

    @bot.message_handler(commands=['versions'])
    def on_versions(message):
        log_event(message.chat.id, uname(message), 'versions')
        if message.chat.id not in EDITOR_CHAT_IDS:
            bot.send_message(message.chat.id, '⛔ Только для редакторов.')
            return
        try:
            items = _git_log(10)
            lines = ['📜 *Последние версии*\n']
            for i, it in enumerate(items, 1):
                subj = it['subject'][:60]
                lines.append(f'{i}. `{it["hash"]}` _{it["rel"]}_\n   {subj}')
            lines.append('\nДля отката: /rollback')
            bot.send_message(message.chat.id, '\n'.join(lines), parse_mode='Markdown')
        except Exception as e:
            bot.send_message(message.chat.id, f'❌ Ошибка: {e}')

    @bot.message_handler(commands=['changelog'])
    def on_changelog(message):
        log_event(message.chat.id, uname(message), 'changelog')
        cl_path = os.path.join(REPO_DIR, 'CHANGELOG.md')
        if not os.path.exists(cl_path):
            bot.send_message(message.chat.id, 'CHANGELOG.md отсутствует.')
            return
        with open(cl_path, encoding='utf-8') as f:
            text = f.read()
        if len(text) > 3800:
            buf = BytesIO(text.encode('utf-8'))
            buf.name = 'CHANGELOG.md'
            bot.send_document(message.chat.id, buf, caption='📋 Журнал изменений')
        else:
            bot.send_message(message.chat.id, text, parse_mode=None,
                             disable_web_page_preview=True)

    @bot.message_handler(commands=['rollback'])
    def on_rollback(message):
        log_event(message.chat.id, uname(message), 'rollback_menu')
        if message.chat.id not in EDITOR_CHAT_IDS:
            bot.send_message(message.chat.id, '⛔ Только для редакторов.')
            return
        try:
            items = _git_log(10)
            # offer to revert to commits 2..10 (revert current=1 makes no sense)
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            for it in items[1:6]:  # show 5 choices (most recent revertable)
                label = f'⏪ {it["hash"]}  {it["subject"][:40]}'
                kb.add(telebot.types.InlineKeyboardButton(
                    label, callback_data=f'rb:{it["hash"]}'))
            kb.add(telebot.types.InlineKeyboardButton('✖ Отмена', callback_data='rb:cancel'))
            bot.send_message(message.chat.id,
                '↩️ *Откат к версии*\n\nВыбери версию, к которой откатиться. '
                'Все коммиты после неё будут отменены через `git revert` '
                '(история сохранится).',
                parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            bot.send_message(message.chat.id, f'❌ Ошибка: {e}')

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith('rb:'))
    def on_rollback_cb(call):
        if call.message.chat.id not in EDITOR_CHAT_IDS:
            bot.answer_callback_query(call.id, '⛔ Только редакторы.')
            return
        action = call.data.split(':', 1)[1]
        if action == 'cancel':
            bot.answer_callback_query(call.id, 'Отменено')
            bot.edit_message_text('✖ Отмена', call.message.chat.id,
                                  call.message.message_id)
            return
        if not action.startswith('confirm:'):
            target = action
            kb = telebot.types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                telebot.types.InlineKeyboardButton('✅ Подтвердить',
                                                    callback_data=f'rb:confirm:{target}'),
                telebot.types.InlineKeyboardButton('✖ Отмена',
                                                    callback_data='rb:cancel'),
            )
            try:
                items = _git_log(10)
                tinfo = next((i for i in items if i['hash'] == target), None)
                subj = tinfo['subject'] if tinfo else '?'
            except Exception:
                subj = '?'
            bot.edit_message_text(
                f'⚠️ Откатиться к `{target}`?\n_{subj}_\n\n'
                'Будет создан НОВЫЙ commit, отменяющий все правки выше '
                'этой версии. site\\_edits.json НЕ затронут.',
                call.message.chat.id, call.message.message_id,
                parse_mode='Markdown', reply_markup=kb)
            return
        # action == 'confirm:<hash>'
        target = action.split(':', 1)[1]
        log_event(call.message.chat.id, '', 'rollback_exec', {'to': target})
        try:
            ahead = _git('rev-list', '--count', f'{target}..HEAD')
            n_ahead = int(ahead)
            if n_ahead == 0:
                bot.edit_message_text('Уже на этой версии.',
                                      call.message.chat.id, call.message.message_id)
                return
            # revert each commit ahead, preserving site_edits.json
            _git('reset', '--hard', 'HEAD')
            _git('revert', '--no-edit', '--no-commit', f'{target}..HEAD')
            # restore site_edits.json from current working tree (uncommitted edit)
            _git('checkout', 'HEAD', '--', 'site_edits.json', check=False)
            _git('-c', 'user.email=bot@local', '-c', 'user.name=propferma-bot',
                 'commit', '-m', f'rollback: revert to {target} via bot')
            push_out = _git('push', 'origin', 'main')
            bot.edit_message_text(
                f'✅ Откачено к `{target}`. Бот перезапускается.\n\n'
                f'`{push_out[-200:] if push_out else "pushed"}`',
                call.message.chat.id, call.message.message_id, parse_mode='Markdown')
            _restart_bot_async()
        except Exception as e:
            bot.edit_message_text(f'❌ Не удалось откатить: {e}',
                                  call.message.chat.id, call.message.message_id)

    bot.set_my_commands([
        telebot.types.BotCommand('/start',     '👋 Запустить бота'),
        telebot.types.BotCommand('/app',       '🏠 Открыть конструктор'),
        telebot.types.BotCommand('/editor',    '✏️ Ссылка редактора (прокси)'),
        telebot.types.BotCommand('/stats',     '📊 Статистика'),
        telebot.types.BotCommand('/versions',  '📜 Версии'),
        telebot.types.BotCommand('/changelog', '📋 Журнал изменений'),
        telebot.types.BotCommand('/rollback',  '↩️ Откат к версии'),
        telebot.types.BotCommand('/restart',   '🔄 Перезапустить'),
    ])

    # Set globally (no chat_id) so button appears for all users without /start
    set_menu_button(None)

    print('Bot started, polling…')
    # Resilient polling: infinity_polling can still raise on certain network
    # errors (RemoteDisconnected, ConnectionError). Wrap in a recovery loop so
    # a transient blip never kills the process.
    while True:
        try:
            bot.infinity_polling(timeout=30, long_polling_timeout=20,
                                 interval=5, skip_pending=True)
            # clean return = intentional stop
            break
        except Exception as e:
            print(f'polling crashed: {e!r} — restarting loop in 10s')
            try:
                time.sleep(10)
            except Exception:
                pass


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
