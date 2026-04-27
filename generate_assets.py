"""
Generates 663 interior-constructor assets via SD WebUI (Juggernaut XL v9).

  3  style previews   1152×640 → 720×400  WebP
 12  room previews    1152×512 → 720×320  WebP
648  pair-picker icons 1024×1024 → 400×400 WebP  (4 rooms × 3 styles)

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
DEFAULT_NEG  = 'people, person, ugly, deformed, noisy, blurry, low resolution, flat lighting, text, watermark, logo, background clutter'
KITCHEN_NEG  = 'background, room environment, floor, floor tiles, people, person, ugly, deformed, noisy, blurry, low resolution, flat lighting, text, watermark, logo'
SCENE_NEG    = 'people, person, ugly, blurry, low quality, text, watermark, oversaturated, dark'

# Alt variants for kitchen slots (same 9 slots, different design direction)
KITCHEN_ALTS = {
    'furniture': [
        'minimalist 3D render of a kitchen island, dark smoked oak base, thick white quartz countertop, built-in cooktop, integrated drawers, isolated on pure white background, soft studio lighting, high quality',
        'minimalist 3D render of a bar stool, matte black steel frame, round solid oak seat, footrest ring, isolated on pure white background, soft studio lighting, high quality',
        'minimalist 3D render of a round dining table, white lacquer top, tapered wooden legs, isolated on pure white background, soft studio lighting, high quality',
        'minimalist 3D render of a wall-mounted kitchen cabinet, frosted glass door fronts, white matte frame, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a tall kitchen pantry cabinet, smooth matte white fronts, push-to-open, thin shadow gap, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a two-tier kitchen rolling cart, stainless steel frame, solid wood top shelf, wire bottom shelf, swivel casters, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of an undermount single-basin stainless steel kitchen sink, brushed finish, seamless countertop edge, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a single-lever kitchen faucet, brushed nickel, high arc spout, pull-down spray, isolated on pure white background, soft studio lighting, product photography',
        'minimalist 3D render of a rectangular white marble cutting board, grey veining, isolated on pure white background, soft studio lighting, product photography',
    ],
    'lighting': [
        'minimalist 3D render of an industrial matte black metal pendant lamp, exposed bulb, over kitchen island, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of under-cabinet LED strip lighting bar, slim aluminum profile, warm white glow, isolated on pure white background, product photography',
        'minimalist 3D render of flush-mount recessed ceiling spotlights, white trim ring, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a matte black adjustable ceiling spotlight, GU10 lamp, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a magnetic LED track system, slim rail with three adjustable spots, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a minimalist gooseneck wall-mounted lamp, matte white, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a square flush LED ceiling panel, warm white, matte white frame, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of a capacitive touch smart light switch, minimalist white glass panel, isolated on pure white background, product photography',
        'minimalist 3D render of a recessed LED plinth light strip, floor-level, soft warm glow line, isolated on pure white background, soft studio lighting',
    ],
    'materials': [
        'minimalist 3D render of a kitchen backsplash tile swatch, white zellige ceramic handmade tiles, irregular texture, isolated on pure white background, product photography',
        'minimalist 3D render of a kitchen countertop slab, polished Calacatta marble, gold grey veining, isolated on pure white background, product photography',
        'minimalist 3D render of kitchen cabinet door fronts, flat matte sage green lacquer, isolated on pure white background, product photography',
        'minimalist 3D render of folded kitchen textiles, cotton waffle-weave dish towels, muted clay color, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of ceramic dinnerware, matte white plates and bowls stacked, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of kitchen counter decor, small terracotta herb pot with rosemary and wooden spoon, isolated on pure white background, soft studio lighting',
        'minimalist 3D render of modern kitchen cabinet bar handles, brushed brass, set of three, isolated on pure white background, product photography',
        'minimalist 3D render of kitchen floor tile swatch, large format matte concrete-look porcelain, isolated on pure white background, product photography',
        'minimalist 3D render of a linen roller blind, light-filtering, natural ecru, isolated on pure white background, soft studio lighting',
    ],
}


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

    # Pair-picker icons — all 4 rooms (living / bedroom / bathroom / kitchen)
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

        # Kitchen pair-picker icons
        for cat_id, prefix in CATS:
            for slot in range(1, 10):
                # main — from catalog
                item = catalog[s]['kitchen'][cat_id].get(str(slot), {})
                main_prompt = item.get('positive', '') if item else ''
                main_neg    = item.get('negative') or KITCHEN_NEG
                # alt — from KITCHEN_ALTS
                cat_idx = [c[0] for c in CATS].index(cat_id)
                alt_prompt = KITCHEN_ALTS[cat_id][slot - 1]
                for variant, prompt, neg in [('main', main_prompt, main_neg),
                                             ('alt',  alt_prompt,  KITCHEN_NEG)]:
                    if not prompt:
                        continue
                    fname = f'{STYLE_FNAME[s]}__kitchen__{cat_id}__{prefix}_{slot}__{variant}.webp'
                    path  = os.path.join(ASSETS, 'icons', 'pair_picker', fname)
                    tasks.append({
                        'desc':  f'icon/pp/{s}/kitchen/{cat_id}/{prefix}_{slot}/{variant}',
                        'prompt': prompt,
                        'neg':   neg,
                        'gen_w': 1024, 'gen_h': 1024,
                        'out_w': 400,  'out_h': 400,
                        'path':  path,
                        'seed':  seed_for(f'icon_{s}_kitchen_{cat_id}_{slot}_{variant}'),
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
