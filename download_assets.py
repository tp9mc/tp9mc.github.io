"""
Downloads 174 interior-constructor images from Pollinations.ai:
  3  style previews  (flux,  720×400)
  9  room previews   (flux,  400×280)
 162 slot icons      (turbo, 400×400)  — style-specific, room-agnostic

Sequential requests with 3-second gap to respect rate limits.
Skips already-downloaded files. Saves as WebP.
"""
import os, sys, time, requests
from urllib.parse import quote
from io import BytesIO
from PIL import Image

REPO    = os.path.dirname(os.path.abspath(__file__))
ASSETS  = os.path.join(REPO, 'assets')
BASE    = 'https://image.pollinations.ai/prompt/'
TIMEOUT = 90
DELAY   = 3      # seconds between requests (respects rate limit)
WAIT429 = 30     # seconds to wait on HTTP 429

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS  = ['living', 'bedroom', 'bathroom']
CATS   = [('furniture','f'), ('lighting','l'), ('materials','m')]

STYLE_DNA = {
    'japandi':        'japandi interior, warm oak wood, natural linen, wabi-sabi, earth tones, zen minimalism',
    'modern_classic': 'modern classic interior, Carrara marble, deep velvet, polished brass, grand symmetry',
    'scandi':         'scandinavian interior, light birch, crisp white, soft wool textiles, hygge coziness',
}
ROOM_DNA = {'living':'living room', 'bedroom':'bedroom', 'bathroom':'bathroom'}

SLOT_ITEMS = {
    'furniture': [
        ['low modular sofa light linen upholstery',      'biomorphic curved sofa sculptural silhouette'],
        ['round solid wood coffee table',                 'rectangular travertine coffee table'],
        ['wide armchair generous armrests soft fabric',  'frameless floating lounge chair minimal'],
        ['open slatted bookshelf light wood',            'closed lacquer storage cabinet sleek'],
        ['solid wood dining table four legs',            'marble top dining table brass base'],
        ['padded upholstered side chair',                'slender metal dining chair geometric'],
        ['tall narrow chest of drawers',                 'long low sideboard horizontal form'],
        ['angled corner storage unit',                   'oval console table island minimal'],
        ['round tufted pouf ottoman linen',              'square low pouf ottoman minimalist'],
    ],
    'lighting': [
        ['slender floor lamp fabric drum shade',         'long arc floor lamp arched arm'],
        ['single globe pendant lamp ceiling',            'cluster of small globe pendant lights'],
        ['classic linen table lamp warm tone',           'sculptural ceramic art table lamp'],
        ['single cone wall sconce minimal',              'double swing-arm wall sconce brass'],
        ['flush recessed ceiling downlights',            'surface mounted track spotlights rail'],
        ['woven wicker ceiling pendant shade',           'tiered crystal chandelier ornate'],
        ['warm amber LED strip diffused glow',           'cool white LED strip crisp line'],
        ['flat geometric wall light minimal',            'layered decorative wall light ornate'],
        ['faceted geometric pendant light',              'matte cone minimal pendant lamp'],
    ],
    'materials': [
        ['light oak herringbone hardwood floor',         'dark smoked walnut plank floor'],
        ['smooth limewash plaster wall texture',         'botanical textured wallpaper pattern'],
        ['matte white painted ceiling',                  'raw exposed concrete ceiling'],
        ['natural linen fabric drape soft folds',        'plush velvet fabric sheen folds'],
        ['sheer linen curtain panel light',              'heavy velvet curtain dramatic drape'],
        ['organic shaped natural jute area rug',         'bold geometric modern area rug'],
        ['solid monochrome scatter cushions',            'embroidered ornament pattern cushions'],
        ['rough textured slate stone tile swatch',       'polished white marble slab veining'],
        ['clear float glass panel transparent',          'sandblasted frosted glass panel'],
    ],
}
STYLE_PREVIEWS = {
    'japandi':        'japandi living room, warm wood furniture, linen sofa, zen atmosphere, afternoon natural light, professional interior photography',
    'modern_classic': 'modern classic living room, marble floors, tufted velvet sofa, brass chandelier, grand symmetrical space, professional photography',
    'scandi':         'scandinavian minimalist living room, light oak floor, white walls, cozy wool throws, natural daylight, professional interior photography',
}

def si(s): return STYLES.index(s)
def ri(r): return ROOMS.index(r)
def ci(c): return [x[0] for x in CATS].index(c)

def purl(prompt, w, h, seed, model='flux'):
    return BASE + quote(prompt) + f'?width={w}&height={h}&seed={seed}&nologo=true&model={model}'

def build_tasks():
    tasks = []
    # style previews
    for s in STYLES:
        tasks.append((
            purl(STYLE_PREVIEWS[s], 720, 400, si(s)*7+42, 'flux'),
            os.path.join(ASSETS, 'styles', f'{s}.webp'),
            f'styles/{s}', 720, 400
        ))
    # room previews
    for s in STYLES:
        for r in ROOMS:
            p = STYLE_DNA[s]+', '+ROOM_DNA[r]+' interior design, professional photography, natural light, high quality'
            tasks.append((
                purl(p, 400, 280, si(s)*100+ri(r)*13+200, 'flux'),
                os.path.join(ASSETS, 'rooms', f'{s}__{r}.webp'),
                f'rooms/{s}__{r}', 400, 280
            ))
    # slot icons — style-specific, NOT room-specific (162 files)
    for s in STYLES:
        for cat_id, px in CATS:
            for idx in range(9):
                slot_id = f'{px}_{idx+1}'
                for vi, variant in enumerate(['main', 'alt']):
                    item = SLOT_ITEMS[cat_id][idx][vi]
                    p    = (item + ', ' + STYLE_DNA[s]
                            + ', product photo, isolated white background, studio light, high quality')
                    seed = si(s)*1000 + ci(cat_id)*20 + idx*2 + vi
                    tasks.append((
                        purl(p, 400, 400, seed, 'turbo'),
                        os.path.join(ASSETS, 'icons', f'{s}__{cat_id}__{slot_id}__{variant}.webp'),
                        f'icons/{s}/{cat_id}/{slot_id}/{variant}', 400, 400
                    ))
    return tasks

def fetch_one(url, path, w, h):
    """Download one image, return (ok, size_or_error)."""
    if os.path.exists(path) and os.path.getsize(path) > 500:
        return True, 'skip'
    for attempt in range(1, 4):
        try:
            r = requests.get(url, timeout=TIMEOUT)
            if r.status_code == 429:
                print(f'  429 – waiting {WAIT429}s…', end='', flush=True)
                time.sleep(WAIT429)
                continue
            if r.status_code != 200:
                return False, f'HTTP {r.status_code}'
            img = Image.open(BytesIO(r.content)).convert('RGB').resize((w, h), Image.LANCZOS)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            img.save(path, 'WEBP', quality=82, method=4)
            return True, f'{os.path.getsize(path)//1024}KB'
        except Exception as e:
            if attempt < 3:
                time.sleep(5)
            else:
                return False, str(e)[:60]
    return False, 'max retries'

def main():
    tasks = build_tasks()
    total = len(tasks)
    ok_count = 0
    failed   = []
    t0 = time.time()
    print(f'Downloading {total} images  (sequential, {DELAY}s gap)\n')

    for i, (url, path, desc, w, h) in enumerate(tasks, 1):
        print(f'[{i:3}/{total}] {desc[:60]:<60}', end=' ', flush=True)
        ok, msg = fetch_one(url, path, w, h)
        elapsed = time.time() - t0
        avg     = elapsed / i
        eta     = int(avg * (total - i))
        print(f'{msg}  ETA {eta//60}m{eta%60:02d}s')
        if ok:
            ok_count += 1
        else:
            failed.append((desc, msg))
        if msg != 'skip':
            time.sleep(DELAY)

    total_time = int(time.time() - t0)
    print(f'\n✓ {ok_count}/{total}  in {total_time//60}m{total_time%60:02d}s')
    if failed:
        print(f'✗ {len(failed)} failed:')
        for d, m in failed:
            print(f'  {d}: {m}')
        sys.exit(1)

if __name__ == '__main__':
    main()
