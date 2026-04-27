"""
Generates 582 interior-constructor assets via SD WebUI (Juggernaut XL v9).

  3  style previews   1152×640 → 720×400  WebP
 12  room previews    1152×512 → 720×320  WebP
486  pair-picker icons 1024×1024 → 400×400 WebP
 81  kit-toggle icons  1024×1024 → 400×400 WebP

Euler / Simple / CFG 8 / 20 steps. Skips existing files.
"""
import os, sys, time, json, hashlib, requests, base64
from io import BytesIO
from PIL import Image

REPO    = os.path.dirname(os.path.abspath(__file__))
ASSETS  = os.path.join(REPO, 'assets')
CATALOG = os.path.join(os.path.dirname(REPO), '../.claude/projects/-Users-timofeev-sd/memory/../../../tmp/catalog.json')
# fallback path
if not os.path.exists(CATALOG):
    CATALOG = '/tmp/catalog.json'

SD_URL  = 'http://localhost:7860/sdapi/v1/txt2img'
TIMEOUT = 300

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS  = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS   = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm')]

STYLE_FNAME = {'japandi': 'japandi', 'modern_classic': 'modern-classic', 'scandi': 'scandi'}

STYLE_DNA = {
    'japandi':        'japandi style, warm oak wood, natural linen, zen minimalism, earth tones, wabi-sabi',
    'modern_classic': 'modern classic style, Carrara marble, deep velvet, polished brass, grand symmetry',
    'scandi':         'scandinavian style, light birch wood, crisp white walls, soft wool textiles, hygge',
}
ROOM_DNA = {
    'living':   'living room, comfortable seating, coffee table, ambient lighting',
    'bedroom':  'bedroom, bed with pillows, bedside tables, soft warm lighting',
    'bathroom': 'bathroom, clean white fixtures, natural stone, elegant vanity',
    'kitchen':  'kitchen, functional workspace, clean countertops, organized storage',
}
STYLE_PREVIEW_PROMPTS = {
    'japandi':        'japandi living room interior, low oak coffee table, linen sofa, rice paper pendant lamp, zen atmosphere, natural light, professional interior photography, architectural digest',
    'modern_classic': 'modern classic living room interior, marble floors, tufted velvet sofa, brass chandelier, grand symmetrical space, professional interior photography, luxurious',
    'scandi':         'scandinavian minimalist living room interior, light oak floor, white walls, cozy wool throws, natural daylight, professional interior photography, hygge',
}
DEFAULT_NEG = 'people, person, ugly, deformed, noisy, blurry, low resolution, flat lighting, text, watermark, logo, background clutter'
SCENE_NEG   = 'people, person, ugly, blurry, low quality, text, watermark, oversaturated, dark'


def seed_for(key: str) -> int:
    return int(hashlib.md5(key.encode()).hexdigest()[:8], 16)


def generate(prompt, neg, gen_w, gen_h, out_w, out_h, path, seed):
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return 'skip'
    payload = {
        'prompt': prompt,
        'negative_prompt': neg,
        'steps': 20, 'cfg_scale': 8,
        'width': gen_w, 'height': gen_h,
        'sampler_name': 'Euler', 'scheduler': 'Simple',
        'seed': seed,
        'override_settings': {'CLIP_stop_at_last_layers': 1},
    }
    for attempt in range(1, 4):
        try:
            r = requests.post(SD_URL, json=payload, timeout=TIMEOUT)
            if r.status_code != 200:
                if attempt < 3:
                    time.sleep(10)
                    continue
                return f'HTTP {r.status_code}'
            img_data = base64.b64decode(r.json()['images'][0])
            img = Image.open(BytesIO(img_data)).convert('RGB').resize((out_w, out_h), Image.LANCZOS)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path, 'WEBP', quality=85, method=4)
            return f'{os.path.getsize(path) // 1024}KB'
        except Exception as e:
            if attempt < 3:
                time.sleep(10)
            else:
                return str(e)[:60]
    return 'max retries'


def build_tasks(catalog):
    tasks = []

    # Style previews
    for s in STYLES:
        path = os.path.join(ASSETS, 'styles', f'{STYLE_FNAME[s]}.webp')
        tasks.append({
            'desc': f'style/{s}',
            'prompt': STYLE_PREVIEW_PROMPTS[s],
            'neg': SCENE_NEG,
            'gen_w': 1152, 'gen_h': 640,
            'out_w': 720,  'out_h': 400,
            'path': path,
            'seed': seed_for(f'style_{s}'),
        })

    # Room previews
    for s in STYLES:
        for r in ROOMS:
            prompt = (f'{STYLE_DNA[s]}, {ROOM_DNA[r]}, '
                      'professional interior photography, natural light, high quality, wide angle')
            path = os.path.join(ASSETS, 'rooms', f'{STYLE_FNAME[s]}__{r}.webp')
            tasks.append({
                'desc': f'room/{s}__{r}',
                'prompt': prompt,
                'neg': SCENE_NEG,
                'gen_w': 1152, 'gen_h': 512,
                'out_w': 720,  'out_h': 320,
                'path': path,
                'seed': seed_for(f'room_{s}_{r}'),
            })

    # Pair-picker icons (living / bedroom / bathroom)
    for s in STYLES:
        for room in ['living', 'bedroom', 'bathroom']:
            for cat_id, prefix in CATS:
                for slot in range(1, 10):
                    for variant in ['main', 'alt']:
                        key = f'{slot}_{variant}'
                        item = catalog[s][room][cat_id].get(key, {})
                        if not item:
                            continue
                        fname = f'{STYLE_FNAME[s]}__{room}__{cat_id}__{prefix}_{slot}__{variant}.webp'
                        path = os.path.join(ASSETS, 'icons', 'pair_picker', fname)
                        tasks.append({
                            'desc': f'icon/pp/{s}/{room}/{cat_id}/{prefix}_{slot}/{variant}',
                            'prompt': item['positive'],
                            'neg': DEFAULT_NEG,
                            'gen_w': 1024, 'gen_h': 1024,
                            'out_w': 400,  'out_h': 400,
                            'path': path,
                            'seed': seed_for(f'icon_{s}_{room}_{cat_id}_{slot}_{variant}'),
                        })

    # Kit-toggle icons (kitchen)
    for s in STYLES:
        for cat_id, prefix in CATS:
            for slot in range(1, 10):
                key = str(slot)
                item = catalog[s]['kitchen'][cat_id].get(key, {})
                if not item:
                    continue
                fname = f'{STYLE_FNAME[s]}__kitchen__{cat_id}__{prefix}_{slot}.webp'
                path = os.path.join(ASSETS, 'icons', 'kit_toggle', fname)
                neg = item.get('negative') or DEFAULT_NEG
                tasks.append({
                    'desc': f'icon/kt/{s}/kitchen/{cat_id}/{prefix}_{slot}',
                    'prompt': item['positive'],
                    'neg': neg,
                    'gen_w': 1024, 'gen_h': 1024,
                    'out_w': 400,  'out_h': 400,
                    'path': path,
                    'seed': seed_for(f'icon_{s}_kitchen_{cat_id}_{slot}'),
                })

    return tasks


def main():
    with open(CATALOG, encoding='utf-8') as f:
        data = json.load(f)
    catalog = data['items']

    tasks = build_tasks(catalog)
    total = len(tasks)
    print(f'Total tasks: {total}')
    print(f'  Style previews:      {sum(1 for t in tasks if t["desc"].startswith("style/"))}')
    print(f'  Room previews:       {sum(1 for t in tasks if t["desc"].startswith("room/"))}')
    print(f'  Pair-picker icons:   {sum(1 for t in tasks if "pp/" in t["desc"])}')
    print(f'  Kit-toggle icons:    {sum(1 for t in tasks if "kt/" in t["desc"])}')
    print()

    ok_count = 0
    failed = []
    t0 = time.time()

    for i, task in enumerate(tasks, 1):
        print(f'[{i:3}/{total}] {task["desc"][:65]:<65}', end=' ', flush=True)
        result = generate(
            task['prompt'], task['neg'],
            task['gen_w'], task['gen_h'],
            task['out_w'], task['out_h'],
            task['path'], task['seed'],
        )
        elapsed = time.time() - t0
        avg = elapsed / i
        eta = int(avg * (total - i))
        print(f'{result}  ETA {eta // 60}m{eta % 60:02d}s')
        if result in ('skip',) or result.endswith('KB'):
            ok_count += 1
        else:
            failed.append((task['desc'], result))

    total_time = int(time.time() - t0)
    print(f'\n✓ {ok_count}/{total}  in {total_time // 60}m{total_time % 60:02d}s')
    if failed:
        print(f'✗ {len(failed)} failed:')
        for d, m in failed:
            print(f'  {d}: {m}')
        sys.exit(1)


if __name__ == '__main__':
    main()
