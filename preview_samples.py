"""
Generate 12 sample BEFORE/AFTER pairs (one representative per cell-row)
for visual comparison between old prompts.json and new prompts_v2.json.

Output: preview_samples/ — 24 images + index.html
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import requests
from PIL import Image

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)
from bot_secrets import HF_TOKEN  # noqa: E402

V1 = json.load(open(os.path.join(REPO, 'prompts_v1_for_compare.json'), encoding='utf-8'))
V2 = json.load(open(os.path.join(REPO, 'prompts_v2.json'), encoding='utf-8'))
OUT = os.path.join(REPO, 'preview_samples')
os.makedirs(OUT, exist_ok=True)

HF_URL = 'https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell'

# 12 representative slots — one per (style, room) — pick a visible furniture/material item
SAMPLES = [
    'opt-japandi-living-furniture-0-main',      # sofa
    'opt-japandi-bedroom-furniture-0-main',     # bed
    'opt-japandi-bathroom-materials-0-main',    # floor/wall
    'opt-japandi-kitchen-furniture-0-main',     # island
    'opt-modern_classic-living-furniture-0-main',
    'opt-modern_classic-bedroom-furniture-0-main',
    'opt-modern_classic-bathroom-materials-0-main',
    'opt-modern_classic-kitchen-furniture-0-main',
    'opt-scandi-living-furniture-0-main',
    'opt-scandi-bedroom-furniture-0-main',
    'opt-scandi-bathroom-materials-0-main',
    'opt-scandi-kitchen-furniture-0-main',
]


def generate(prompt: str, path: str) -> str:
    r = requests.post(HF_URL, headers={'Authorization': f'Bearer {HF_TOKEN}'},
                      json={'inputs': prompt, 'parameters': {'width': 1024, 'height': 1024}},
                      timeout=120)
    if r.status_code != 200:
        return f'HTTP {r.status_code}'
    img = Image.open(BytesIO(r.content)).convert('RGB').resize((400, 400), Image.LANCZOS)
    img.save(path, 'WEBP', quality=85, method=4)
    return 'OK'


def task(args):
    key, version, prompt = args
    safe = key.replace('-', '_')
    path = os.path.join(OUT, f'{safe}_{version}.webp')
    t0 = time.time()
    result = generate(prompt, path)
    return key, version, result, time.time() - t0


def main():
    jobs = []
    for k in SAMPLES:
        if k in V1: jobs.append((k, 'v1', V1[k]))
        if k in V2: jobs.append((k, 'v2', V2[k]))
        else: print(f'SKIP v2 (missing): {k}')
    print(f'Sample jobs: {len(jobs)}')

    results = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(task, j): j for j in jobs}
        for f in as_completed(futs):
            key, version, result, dur = f.result()
            results[(key, version)] = result
            print(f'  {key} [{version}] {result}  ({dur:.1f}s)')

    # build index.html
    html = ['<!doctype html><meta charset="utf-8">',
            '<title>Prompt v1 → v2 visual comparison</title>',
            '<style>body{font-family:sans-serif;background:#222;color:#eee;padding:16px}',
            'table{border-collapse:collapse;margin-bottom:24px}',
            'td{padding:8px;vertical-align:top;border:1px solid #444}',
            'img{display:block;width:400px;height:400px;object-fit:cover}',
            'pre{white-space:pre-wrap;max-width:400px;font-size:11px;color:#aaa}',
            'h2{border-bottom:1px solid #444;padding-bottom:4px}</style>',
            '<h1>Prompt rewrite — v1 (current live) vs v2 (rewritten via palette+graph)</h1>']
    for k in SAMPLES:
        html.append(f'<h2>{k}</h2><table><tr>')
        for ver, label in [('v1', 'BEFORE (v1)'), ('v2', 'AFTER (v2)')]:
            prompt = (V1 if ver == 'v1' else V2).get(k, '(missing)')
            safe = k.replace('-', '_')
            img = f'{safe}_{ver}.webp'
            html.append(f'<td><b>{label}</b><br><img src="{img}"><pre>{prompt}</pre></td>')
        html.append('</tr></table>')

    open(os.path.join(OUT, 'index.html'), 'w').write('\n'.join(html))
    print(f'\nSaved: {OUT}/index.html')
    print(f'Open: file://{OUT}/index.html')


if __name__ == '__main__':
    main()
