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
            ]
        }
    }
}

SLOT_OPTS = {
    "japandi": {
        "living": {
            "furniture": [
                [
                    "Диван",
                    "Диван",
                    "Диван"
                ],
                [
                    "Кресло",
                    "Кресло",
                    "Кресло"
                ],
                [
                    "Журнальный стол",
                    "Журнальный стол",
                    "Журнальный стол"
                ],
                [
                    "Приставной столик",
                    "Приставной столик",
                    "Приставной столик"
                ],
                [
                    "ТВ-зона",
                    "ТВ-зона",
                    "ТВ-зона"
                ],
                [
                    "Стеллаж",
                    "Стеллаж",
                    "Стеллаж"
                ],
                [
                    "Комод/буфет",
                    "Комод/буфет",
                    "Комод/буфет"
                ],
                [
                    "Консоль",
                    "Консоль",
                    "Консоль"
                ],
                [
                    "Пуф",
                    "Пуф",
                    "Пуф"
                ]
            ],
            "lighting": [
                [
                    "Люстра",
                    "Люстра",
                    "Люстра"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Настольная",
                    "Настольная",
                    "Настольная"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Подсветка картин",
                    "Подсветка картин",
                    "Подсветка картин"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Подушки/плед",
                    "Подушки/плед",
                    "Подушки/плед"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Кровать",
                    "Кровать",
                    "Кровать"
                ],
                [
                    "Изголовье",
                    "Изголовье",
                    "Изголовье"
                ],
                [
                    "Прикроватная тумба",
                    "Прикроватная тумба",
                    "Прикроватная тумба"
                ],
                [
                    "Комод",
                    "Комод",
                    "Комод"
                ],
                [
                    "Шкаф",
                    "Шкаф",
                    "Шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Туалетный столик",
                    "Туалетный столик",
                    "Туалетный столик"
                ],
                [
                    "Кресло для чтения",
                    "Кресло для чтения",
                    "Кресло для чтения"
                ],
                [
                    "Зеркало",
                    "Зеркало",
                    "Зеркало"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Прикроватная",
                    "Прикроватная",
                    "Прикроватная"
                ],
                [
                    "Бра для чтения",
                    "Бра для чтения",
                    "Бра для чтения"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-изголовье",
                    "LED-изголовье",
                    "LED-изголовье"
                ],
                [
                    "Свет в шкафу",
                    "Свет в шкафу",
                    "Свет в шкафу"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Постельный текстиль",
                    "Постельный текстиль",
                    "Постельный текстиль"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы blackout",
                    "Шторы blackout",
                    "Шторы blackout"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Тумба",
                    "Тумба",
                    "Тумба"
                ],
                [
                    "Раковина",
                    "Раковина",
                    "Раковина"
                ],
                [
                    "Ванна",
                    "Ванна",
                    "Ванна"
                ],
                [
                    "Душевая",
                    "Душевая",
                    "Душевая"
                ],
                [
                    "Унитаз",
                    "Унитаз",
                    "Унитаз"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Зеркало-шкаф",
                    "Зеркало-шкаф",
                    "Зеркало-шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Полотенцедержатель",
                    "Полотенцедержатель",
                    "Полотенцедержатель"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Свет зеркала",
                    "Свет зеркала",
                    "Свет зеркала"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Свет ниши",
                    "Свет ниши",
                    "Свет ниши"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Подсветка зеркала",
                    "Подсветка зеркала",
                    "Подсветка зеркала"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Плитка пола",
                    "Плитка пола",
                    "Плитка пола"
                ],
                [
                    "Плитка стен",
                    "Плитка стен",
                    "Плитка стен"
                ],
                [
                    "Отделка душа",
                    "Отделка душа",
                    "Отделка душа"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Смеситель-финиш",
                    "Смеситель-финиш",
                    "Смеситель-финиш"
                ],
                [
                    "Полотенца",
                    "Полотенца",
                    "Полотенца"
                ],
                [
                    "Коврик",
                    "Коврик",
                    "Коврик"
                ],
                [
                    "Стекло душа",
                    "Стекло душа",
                    "Стекло душа"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Нижние шкафы",
                    "Нижние шкафы",
                    "Нижние шкафы"
                ],
                [
                    "Верхние шкафы",
                    "Верхние шкафы",
                    "Верхние шкафы"
                ],
                [
                    "Остров",
                    "Остров",
                    "Остров"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Барные стулья",
                    "Барные стулья",
                    "Барные стулья"
                ],
                [
                    "Обеденный стол",
                    "Обеденный стол",
                    "Обеденный стол"
                ],
                [
                    "Стулья",
                    "Стулья",
                    "Стулья"
                ],
                [
                    "Мойка+смеситель",
                    "Мойка+смеситель",
                    "Мойка+смеситель"
                ],
                [
                    "Открытые полки",
                    "Открытые полки",
                    "Открытые полки"
                ]
            ],
            "lighting": [
                [
                    "Общий",
                    "Общий",
                    "Общий"
                ],
                [
                    "Подвес над островом",
                    "Подвес над островом",
                    "Подвес над островом"
                ],
                [
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Трек",
                    "Трек",
                    "Трек"
                ],
                [
                    "Подсветка цоколя",
                    "Подсветка цоколя",
                    "Подсветка цоколя"
                ],
                [
                    "Свет в шкафах",
                    "Свет в шкафах",
                    "Свет в шкафах"
                ],
                [
                    "Обеденный подвес",
                    "Обеденный подвес",
                    "Обеденный подвес"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Фартук",
                    "Фартук",
                    "Фартук"
                ],
                [
                    "Фасады",
                    "Фасады",
                    "Фасады"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Ручки",
                    "Ручки",
                    "Ручки"
                ],
                [
                    "Материал мойки",
                    "Материал мойки",
                    "Материал мойки"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Текстиль",
                    "Текстиль",
                    "Текстиль"
                ]
            ]
        }
    },
    "modern_classic": {
        "living": {
            "furniture": [
                [
                    "Диван",
                    "Диван",
                    "Диван"
                ],
                [
                    "Кресло",
                    "Кресло",
                    "Кресло"
                ],
                [
                    "Журнальный стол",
                    "Журнальный стол",
                    "Журнальный стол"
                ],
                [
                    "Приставной столик",
                    "Приставной столик",
                    "Приставной столик"
                ],
                [
                    "ТВ-зона",
                    "ТВ-зона",
                    "ТВ-зона"
                ],
                [
                    "Стеллаж",
                    "Стеллаж",
                    "Стеллаж"
                ],
                [
                    "Комод/буфет",
                    "Комод/буфет",
                    "Комод/буфет"
                ],
                [
                    "Консоль",
                    "Консоль",
                    "Консоль"
                ],
                [
                    "Пуф",
                    "Пуф",
                    "Пуф"
                ]
            ],
            "lighting": [
                [
                    "Люстра",
                    "Люстра",
                    "Люстра"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Настольная",
                    "Настольная",
                    "Настольная"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Подсветка картин",
                    "Подсветка картин",
                    "Подсветка картин"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Подушки/плед",
                    "Подушки/плед",
                    "Подушки/плед"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Кровать",
                    "Кровать",
                    "Кровать"
                ],
                [
                    "Изголовье",
                    "Изголовье",
                    "Изголовье"
                ],
                [
                    "Прикроватная тумба",
                    "Прикроватная тумба",
                    "Прикроватная тумба"
                ],
                [
                    "Комод",
                    "Комод",
                    "Комод"
                ],
                [
                    "Шкаф",
                    "Шкаф",
                    "Шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Туалетный столик",
                    "Туалетный столик",
                    "Туалетный столик"
                ],
                [
                    "Кресло для чтения",
                    "Кресло для чтения",
                    "Кресло для чтения"
                ],
                [
                    "Зеркало",
                    "Зеркало",
                    "Зеркало"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Прикроватная",
                    "Прикроватная",
                    "Прикроватная"
                ],
                [
                    "Бра для чтения",
                    "Бра для чтения",
                    "Бра для чтения"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-изголовье",
                    "LED-изголовье",
                    "LED-изголовье"
                ],
                [
                    "Свет в шкафу",
                    "Свет в шкафу",
                    "Свет в шкафу"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Постельный текстиль",
                    "Постельный текстиль",
                    "Постельный текстиль"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы blackout",
                    "Шторы blackout",
                    "Шторы blackout"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Тумба",
                    "Тумба",
                    "Тумба"
                ],
                [
                    "Раковина",
                    "Раковина",
                    "Раковина"
                ],
                [
                    "Ванна",
                    "Ванна",
                    "Ванна"
                ],
                [
                    "Душевая",
                    "Душевая",
                    "Душевая"
                ],
                [
                    "Унитаз",
                    "Унитаз",
                    "Унитаз"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Зеркало-шкаф",
                    "Зеркало-шкаф",
                    "Зеркало-шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Полотенцедержатель",
                    "Полотенцедержатель",
                    "Полотенцедержатель"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Свет зеркала",
                    "Свет зеркала",
                    "Свет зеркала"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Свет ниши",
                    "Свет ниши",
                    "Свет ниши"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Подсветка зеркала",
                    "Подсветка зеркала",
                    "Подсветка зеркала"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Плитка пола",
                    "Плитка пола",
                    "Плитка пола"
                ],
                [
                    "Плитка стен",
                    "Плитка стен",
                    "Плитка стен"
                ],
                [
                    "Отделка душа",
                    "Отделка душа",
                    "Отделка душа"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Смеситель-финиш",
                    "Смеситель-финиш",
                    "Смеситель-финиш"
                ],
                [
                    "Полотенца",
                    "Полотенца",
                    "Полотенца"
                ],
                [
                    "Коврик",
                    "Коврик",
                    "Коврик"
                ],
                [
                    "Стекло душа",
                    "Стекло душа",
                    "Стекло душа"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Нижние шкафы",
                    "Нижние шкафы",
                    "Нижние шкафы"
                ],
                [
                    "Верхние шкафы",
                    "Верхние шкафы",
                    "Верхние шкафы"
                ],
                [
                    "Остров",
                    "Остров",
                    "Остров"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Барные стулья",
                    "Барные стулья",
                    "Барные стулья"
                ],
                [
                    "Обеденный стол",
                    "Обеденный стол",
                    "Обеденный стол"
                ],
                [
                    "Стулья",
                    "Стулья",
                    "Стулья"
                ],
                [
                    "Мойка+смеситель",
                    "Мойка+смеситель",
                    "Мойка+смеситель"
                ],
                [
                    "Открытые полки",
                    "Открытые полки",
                    "Открытые полки"
                ]
            ],
            "lighting": [
                [
                    "Общий",
                    "Общий",
                    "Общий"
                ],
                [
                    "Подвес над островом",
                    "Подвес над островом",
                    "Подвес над островом"
                ],
                [
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Трек",
                    "Трек",
                    "Трек"
                ],
                [
                    "Подсветка цоколя",
                    "Подсветка цоколя",
                    "Подсветка цоколя"
                ],
                [
                    "Свет в шкафах",
                    "Свет в шкафах",
                    "Свет в шкафах"
                ],
                [
                    "Обеденный подвес",
                    "Обеденный подвес",
                    "Обеденный подвес"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Фартук",
                    "Фартук",
                    "Фартук"
                ],
                [
                    "Фасады",
                    "Фасады",
                    "Фасады"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Ручки",
                    "Ручки",
                    "Ручки"
                ],
                [
                    "Материал мойки",
                    "Материал мойки",
                    "Материал мойки"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Текстиль",
                    "Текстиль",
                    "Текстиль"
                ]
            ]
        }
    },
    "scandi": {
        "living": {
            "furniture": [
                [
                    "Диван",
                    "Диван",
                    "Диван"
                ],
                [
                    "Кресло",
                    "Кресло",
                    "Кресло"
                ],
                [
                    "Журнальный стол",
                    "Журнальный стол",
                    "Журнальный стол"
                ],
                [
                    "Приставной столик",
                    "Приставной столик",
                    "Приставной столик"
                ],
                [
                    "ТВ-зона",
                    "ТВ-зона",
                    "ТВ-зона"
                ],
                [
                    "Стеллаж",
                    "Стеллаж",
                    "Стеллаж"
                ],
                [
                    "Комод/буфет",
                    "Комод/буфет",
                    "Комод/буфет"
                ],
                [
                    "Консоль",
                    "Консоль",
                    "Консоль"
                ],
                [
                    "Пуф",
                    "Пуф",
                    "Пуф"
                ]
            ],
            "lighting": [
                [
                    "Люстра",
                    "Люстра",
                    "Люстра"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Настольная",
                    "Настольная",
                    "Настольная"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Подсветка картин",
                    "Подсветка картин",
                    "Подсветка картин"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Подушки/плед",
                    "Подушки/плед",
                    "Подушки/плед"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bedroom": {
            "furniture": [
                [
                    "Кровать",
                    "Кровать",
                    "Кровать"
                ],
                [
                    "Изголовье",
                    "Изголовье",
                    "Изголовье"
                ],
                [
                    "Прикроватная тумба",
                    "Прикроватная тумба",
                    "Прикроватная тумба"
                ],
                [
                    "Комод",
                    "Комод",
                    "Комод"
                ],
                [
                    "Шкаф",
                    "Шкаф",
                    "Шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Туалетный столик",
                    "Туалетный столик",
                    "Туалетный столик"
                ],
                [
                    "Кресло для чтения",
                    "Кресло для чтения",
                    "Кресло для чтения"
                ],
                [
                    "Зеркало",
                    "Зеркало",
                    "Зеркало"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Подвес",
                    "Подвес",
                    "Подвес"
                ],
                [
                    "Прикроватная",
                    "Прикроватная",
                    "Прикроватная"
                ],
                [
                    "Бра для чтения",
                    "Бра для чтения",
                    "Бра для чтения"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "LED-изголовье",
                    "LED-изголовье",
                    "LED-изголовье"
                ],
                [
                    "Свет в шкафу",
                    "Свет в шкафу",
                    "Свет в шкафу"
                ],
                [
                    "Торшер",
                    "Торшер",
                    "Торшер"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Акцентная стена",
                    "Акцентная стена",
                    "Акцентная стена"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Постельный текстиль",
                    "Постельный текстиль",
                    "Постельный текстиль"
                ],
                [
                    "Ковёр",
                    "Ковёр",
                    "Ковёр"
                ],
                [
                    "Шторы blackout",
                    "Шторы blackout",
                    "Шторы blackout"
                ],
                [
                    "Обивка",
                    "Обивка",
                    "Обивка"
                ],
                [
                    "Фурнитура",
                    "Фурнитура",
                    "Фурнитура"
                ]
            ]
        },
        "bathroom": {
            "furniture": [
                [
                    "Тумба",
                    "Тумба",
                    "Тумба"
                ],
                [
                    "Раковина",
                    "Раковина",
                    "Раковина"
                ],
                [
                    "Ванна",
                    "Ванна",
                    "Ванна"
                ],
                [
                    "Душевая",
                    "Душевая",
                    "Душевая"
                ],
                [
                    "Унитаз",
                    "Унитаз",
                    "Унитаз"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Зеркало-шкаф",
                    "Зеркало-шкаф",
                    "Зеркало-шкаф"
                ],
                [
                    "Банкетка",
                    "Банкетка",
                    "Банкетка"
                ],
                [
                    "Полотенцедержатель",
                    "Полотенцедержатель",
                    "Полотенцедержатель"
                ]
            ],
            "lighting": [
                [
                    "Потолочный",
                    "Потолочный",
                    "Потолочный"
                ],
                [
                    "Свет зеркала",
                    "Свет зеркала",
                    "Свет зеркала"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Свет ниши",
                    "Свет ниши",
                    "Свет ниши"
                ],
                [
                    "Бра",
                    "Бра",
                    "Бра"
                ],
                [
                    "Подсветка зеркала",
                    "Подсветка зеркала",
                    "Подсветка зеркала"
                ],
                [
                    "LED-карниз",
                    "LED-карниз",
                    "LED-карниз"
                ],
                [
                    "Ночник",
                    "Ночник",
                    "Ночник"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Плитка пола",
                    "Плитка пола",
                    "Плитка пола"
                ],
                [
                    "Плитка стен",
                    "Плитка стен",
                    "Плитка стен"
                ],
                [
                    "Отделка душа",
                    "Отделка душа",
                    "Отделка душа"
                ],
                [
                    "Потолок",
                    "Потолок",
                    "Потолок"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Смеситель-финиш",
                    "Смеситель-финиш",
                    "Смеситель-финиш"
                ],
                [
                    "Полотенца",
                    "Полотенца",
                    "Полотенца"
                ],
                [
                    "Коврик",
                    "Коврик",
                    "Коврик"
                ],
                [
                    "Стекло душа",
                    "Стекло душа",
                    "Стекло душа"
                ]
            ]
        },
        "kitchen": {
            "furniture": [
                [
                    "Нижние шкафы",
                    "Нижние шкафы",
                    "Нижние шкафы"
                ],
                [
                    "Верхние шкафы",
                    "Верхние шкафы",
                    "Верхние шкафы"
                ],
                [
                    "Остров",
                    "Остров",
                    "Остров"
                ],
                [
                    "Пенал",
                    "Пенал",
                    "Пенал"
                ],
                [
                    "Барные стулья",
                    "Барные стулья",
                    "Барные стулья"
                ],
                [
                    "Обеденный стол",
                    "Обеденный стол",
                    "Обеденный стол"
                ],
                [
                    "Стулья",
                    "Стулья",
                    "Стулья"
                ],
                [
                    "Мойка+смеситель",
                    "Мойка+смеситель",
                    "Мойка+смеситель"
                ],
                [
                    "Открытые полки",
                    "Открытые полки",
                    "Открытые полки"
                ]
            ],
            "lighting": [
                [
                    "Общий",
                    "Общий",
                    "Общий"
                ],
                [
                    "Подвес над островом",
                    "Подвес над островом",
                    "Подвес над островом"
                ],
                [
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны",
                    "Подсветка рабочей зоны"
                ],
                [
                    "Споты",
                    "Споты",
                    "Споты"
                ],
                [
                    "Трек",
                    "Трек",
                    "Трек"
                ],
                [
                    "Подсветка цоколя",
                    "Подсветка цоколя",
                    "Подсветка цоколя"
                ],
                [
                    "Свет в шкафах",
                    "Свет в шкафах",
                    "Свет в шкафах"
                ],
                [
                    "Обеденный подвес",
                    "Обеденный подвес",
                    "Обеденный подвес"
                ],
                [
                    "Диммер",
                    "Диммер",
                    "Диммер"
                ]
            ],
            "materials": [
                [
                    "Пол",
                    "Пол",
                    "Пол"
                ],
                [
                    "Столешница",
                    "Столешница",
                    "Столешница"
                ],
                [
                    "Фартук",
                    "Фартук",
                    "Фартук"
                ],
                [
                    "Фасады",
                    "Фасады",
                    "Фасады"
                ],
                [
                    "Стены",
                    "Стены",
                    "Стены"
                ],
                [
                    "Ручки",
                    "Ручки",
                    "Ручки"
                ],
                [
                    "Материал мойки",
                    "Материал мойки",
                    "Материал мойки"
                ],
                [
                    "Шторы",
                    "Шторы",
                    "Шторы"
                ],
                [
                    "Текстиль",
                    "Текстиль",
                    "Текстиль"
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
