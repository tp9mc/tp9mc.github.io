"""
Regenerates 663 interior-constructor assets via HuggingFace FLUX.1-schnell.

  3  style previews   1152×640 → 720×400  WebP
 12  room previews    1152×512 → 720×320  WebP
648  pair-picker icons 1024×1024 → 400×400 WebP

~2s per image, ~3 workers → ~5 min total.
Overwrites existing files. Skips nothing (full regen).
"""
import os, sys, time, json, requests
from io import BytesIO
from PIL import Image
from concurrent.futures import ThreadPoolExecutor, as_completed

REPO    = os.path.dirname(os.path.abspath(__file__))
ASSETS  = os.path.join(REPO, 'assets')
CATALOG = '/tmp/catalog.json'
if not os.path.exists(CATALOG):
    CATALOG = os.path.join(os.path.dirname(REPO), '../tmp/catalog.json')

HF_TOKEN = None
try:
    sys.path.insert(0, REPO)
    from bot_secrets import HF_TOKEN
except Exception:
    pass
if not HF_TOKEN:
    HF_TOKEN = os.environ.get('HF_TOKEN', '')
if not HF_TOKEN:
    sys.exit('No HF_TOKEN found in bot_secrets.py or environment')

HF_URL  = 'https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell'
WORKERS = 3

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS  = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS   = [('furniture', 'f'), ('lighting', 'l'), ('materials', 'm')]
STYLE_FNAME = {'japandi': 'japandi', 'modern_classic': 'modern-classic', 'scandi': 'scandi'}

STYLE_PREVIEW_PROMPTS = {
    'japandi':        'japandi living room interior, low oak coffee table, linen sofa, rice paper pendant lamp, zen atmosphere, natural light, professional interior photography, architectural digest',
    'modern_classic': 'modern classic living room interior, marble floors, tufted velvet sofa, brass chandelier, grand symmetrical space, professional interior photography, luxurious',
    'scandi':         'scandinavian minimalist living room interior, light oak floor, white walls, cozy wool throws, natural daylight, professional interior photography, hygge',
}
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


def generate_hf(prompt, gen_w, gen_h, out_w, out_h, path):
    r = requests.post(
        HF_URL,
        headers={'Authorization': f'Bearer {HF_TOKEN}'},
        json={'inputs': prompt, 'parameters': {'width': gen_w, 'height': gen_h}},
        timeout=120,
    )
    if r.status_code != 200:
        return f'HTTP {r.status_code}: {r.text[:60]}'
    img = Image.open(BytesIO(r.content)).convert('RGB').resize((out_w, out_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, 'WEBP', quality=85, method=4)
    return f'{os.path.getsize(path) // 1024}KB'


def build_tasks(catalog):
    tasks = []

    for s in STYLES:
        path = os.path.join(ASSETS, 'styles', f'{STYLE_FNAME[s]}.webp')
        tasks.append({'desc': f'style/{s}', 'prompt': STYLE_PREVIEW_PROMPTS[s],
                      'gen_w': 1152, 'gen_h': 640, 'out_w': 720, 'out_h': 400, 'path': path})

    for s in STYLES:
        for r in ROOMS:
            prompt = (f'{STYLE_DNA[s]}, {ROOM_DNA[r]}, '
                      'professional interior photography, natural light, high quality, wide angle')
            path = os.path.join(ASSETS, 'rooms', f'{STYLE_FNAME[s]}__{r}.webp')
            tasks.append({'desc': f'room/{s}__{r}', 'prompt': prompt,
                          'gen_w': 1152, 'gen_h': 512, 'out_w': 720, 'out_h': 320, 'path': path})

    for s in STYLES:
        for room in ['living', 'bedroom', 'bathroom']:
            for cat_id, prefix in CATS:
                for slot in range(1, 10):
                    for variant in ['main', 'alt']:
                        item = catalog[s][room][cat_id].get(f'{slot}_{variant}', {})
                        if not item:
                            continue
                        path = os.path.join(ASSETS, 'items', STYLE_FNAME[s], room, cat_id,
                                            f'{slot}__{variant}.webp')
                        tasks.append({'desc': f'icon/{s}/{room}/{cat_id}/{slot}/{variant}',
                                      'prompt': item['positive'],
                                      'gen_w': 1024, 'gen_h': 1024, 'out_w': 400, 'out_h': 400,
                                      'path': path})

        for cat_id, prefix in CATS:
            for slot in range(1, 10):
                item = catalog[s]['kitchen'][cat_id].get(str(slot), {})
                main_prompt = item.get('positive', '') if item else ''
                alt_prompt  = KITCHEN_ALTS[cat_id][slot - 1]
                for variant, prompt in [('main', main_prompt), ('alt', alt_prompt)]:
                    if not prompt:
                        continue
                    path = os.path.join(ASSETS, 'items', STYLE_FNAME[s], 'kitchen', cat_id,
                                        f'{slot}__{variant}.webp')
                    tasks.append({'desc': f'icon/{s}/kitchen/{cat_id}/{slot}/{variant}',
                                  'prompt': prompt,
                                  'gen_w': 1024, 'gen_h': 1024, 'out_w': 400, 'out_h': 400,
                                  'path': path})

    return tasks


def run_task(args):
    idx, total, task = args
    t0 = time.time()
    result = generate_hf(task['prompt'], task['gen_w'], task['gen_h'],
                         task['out_w'], task['out_h'], task['path'])
    return idx, task['desc'], result, time.time() - t0


def main():
    with open(CATALOG, encoding='utf-8') as f:
        catalog = json.load(f)['items']

    tasks = build_tasks(catalog)
    total = len(tasks)
    print(f'Tasks: {total}  |  Workers: {WORKERS}  |  Model: FLUX.1-schnell')
    print(f'  Style previews:    {sum(1 for t in tasks if t["desc"].startswith("style/"))}')
    print(f'  Room previews:     {sum(1 for t in tasks if t["desc"].startswith("room/"))}')
    print(f'  Pair-picker icons: {sum(1 for t in tasks if t["desc"].startswith("icon/"))}')
    print()

    ok, failed = 0, []
    t0 = time.time()
    done = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(run_task, (i, total, t)): i
                   for i, t in enumerate(tasks, 1)}
        for fut in as_completed(futures):
            idx, desc, result, dur = fut.result()
            done += 1
            elapsed = time.time() - t0
            avg = elapsed / done
            eta = int(avg * (total - done))
            status = '✓' if result.endswith('KB') else '✗'
            print(f'[{done:3}/{total}] {status} {desc[:55]:<55} {result:<12} {dur:.1f}s  ETA {eta//60}m{eta%60:02d}s')
            if result.endswith('KB'):
                ok += 1
            else:
                failed.append((desc, result))

    total_time = int(time.time() - t0)
    print(f'\n✓ {ok}/{total}  in {total_time//60}m{total_time%60:02d}s')
    if failed:
        print(f'✗ {len(failed)} failed:')
        for d, m in failed:
            print(f'  {d}: {m}')
        sys.exit(1)


if __name__ == '__main__':
    main()
