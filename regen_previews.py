"""
Regen 3 style + 12 room preview images using palette-aligned scene prompts.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import HF_TOKEN  # noqa: E402

PALETTE = json.loads((REPO / 'palette.json').read_text(encoding='utf-8'))
HF_URL = 'https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell'
STYLE_FNAME = {'japandi': 'japandi', 'modern_classic': 'modern-classic', 'scandi': 'scandi'}


def style_prompt(style):
    p = PALETTE[style]['living']
    dna = PALETTE[style]['_dna']
    return (f'{dna}. Wide interior shot of a beautiful living room: '
            f'{p["wood_primary"]} on the floor, {p["fabric_main"]} sofa, '
            f'{p["wall"]} walls, {p["accent_color"]}, '
            f'{p["metal_primary"]} accents, natural light, architectural photography, '
            f'magazine cover quality, 8k.')


def room_prompt(style, room):
    p = PALETTE[style][room]
    dna = PALETTE[style]['_dna']
    if room == 'living':
        body = (f'{p["fabric_main"]} sofa, {p["wood_primary"]} floor, '
                f'{p["wall"]} walls, {p["stone"]} accents, {p["accent_color"]}')
    elif room == 'bedroom':
        body = (f'bed with {p["fabric_main"]} linens and {p["fabric_accent"]}, '
                f'{p["wood_primary"]} nightstands, {p["wall"]} walls, soft warm lighting')
    elif room == 'bathroom':
        body = (f'{p["stone"]} surfaces, {p["wood_primary"]} accents, '
                f'{p["metal_primary"]} fixtures, {p["wall"]} walls, clean elegant')
    else:  # kitchen
        body = (f'{p["wood_primary"]} cabinets, {p["stone"]} countertop, '
                f'{p["metal_primary"]} hardware, {p["wall"]} walls, functional minimalist')
    return (f'{dna}. Wide interior photograph of a {room}: {body}, natural daylight, '
            f'architectural digest, professional interior photography, no people, 8k.')


def generate(prompt, gen_w, gen_h, out_w, out_h, path):
    r = requests.post(HF_URL, headers={'Authorization': f'Bearer {HF_TOKEN}'},
                      json={'inputs': prompt, 'parameters': {'width': gen_w, 'height': gen_h}},
                      timeout=120)
    if r.status_code != 200:
        return f'HTTP {r.status_code}'
    img = Image.open(BytesIO(r.content)).convert('RGB').resize((out_w, out_h), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, 'WEBP', quality=85, method=4)
    return f'{os.path.getsize(path) // 1024}KB'


def main():
    tasks = []
    for s in ['japandi', 'modern_classic', 'scandi']:
        tasks.append(('style/' + s, style_prompt(s), 1152, 640, 720, 400,
                      str(REPO / 'assets' / 'styles' / f'{STYLE_FNAME[s]}.webp')))
        for r in ['living', 'bedroom', 'bathroom', 'kitchen']:
            tasks.append((f'room/{s}__{r}', room_prompt(s, r), 1152, 512, 720, 320,
                          str(REPO / 'assets' / 'rooms' / f'{STYLE_FNAME[s]}__{r}.webp')))

    print(f'Tasks: {len(tasks)}')
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(generate, t[1], t[2], t[3], t[4], t[5], t[6]): t for t in tasks}
        for f in as_completed(futs):
            t = futs[f]
            print(f'  {t[0]}: {f.result()}')
    print(f'Done in {int(time.time()-t0)}s')


if __name__ == '__main__':
    main()
