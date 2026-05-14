"""
Regenerate the 648 pair-picker item icons from prompts_v2.json via FLUX.1-schnell.

Reads ONLY items (not style/room previews — those are unchanged).
Saves to assets/items/{style_fname}/{room}/{cat}/{slot}__{variant}.webp.
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
ASSETS = os.path.join(REPO, 'assets')
PROMPTS_FILE = os.path.join(REPO, sys.argv[1] if len(sys.argv) > 1 else 'prompts_v2.json')

sys.path.insert(0, REPO)
from bot_secrets import HF_TOKEN  # noqa: E402

HF_URL = 'https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell'
WORKERS = 3

STYLE_FNAME = {'japandi': 'japandi', 'modern_classic': 'modern-classic', 'scandi': 'scandi'}


def generate_hf(prompt: str, path: str) -> str:
    r = requests.post(
        HF_URL,
        headers={'Authorization': f'Bearer {HF_TOKEN}'},
        json={'inputs': prompt, 'parameters': {'width': 1024, 'height': 1024}},
        timeout=120,
    )
    if r.status_code != 200:
        return f'HTTP {r.status_code}: {r.text[:80]}'
    img = Image.open(BytesIO(r.content)).convert('RGB').resize((400, 400), Image.LANCZOS)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img.save(path, 'WEBP', quality=85, method=4)
    return f'{os.path.getsize(path) // 1024}KB'


def build_tasks() -> list:
    prompts = json.loads(open(PROMPTS_FILE, encoding='utf-8').read())
    tasks = []
    for key, prompt in prompts.items():
        if not key.startswith('opt-'):
            continue
        parts = key.split('-')
        # opt-{style}-{room}-{cat}-{idx}-{variant}
        # but modern_classic has underscore, no hyphen
        if 'modern_classic' in key:
            # canonicalise: replace 'modern_classic' with placeholder, split, restore
            mod_key = key.replace('modern_classic', 'modernclassic')
            parts = mod_key.split('-')
            style = 'modern_classic'
            room, cat, idx, var = parts[2], parts[3], parts[4], parts[5]
        else:
            style = parts[1]
            room, cat, idx, var = parts[2], parts[3], parts[4], parts[5]
        slot = int(idx) + 1
        path = os.path.join(ASSETS, 'items', STYLE_FNAME[style], room, cat,
                            f'{slot}__{var}.webp')
        tasks.append({'desc': f'{style}/{room}/{cat}/{slot}/{var}',
                      'prompt': prompt, 'path': path})
    return tasks


def run_task(args):
    idx, total, task = args
    t0 = time.time()
    result = generate_hf(task['prompt'], task['path'])
    return idx, task['desc'], result, time.time() - t0


def main():
    tasks = build_tasks()
    total = len(tasks)
    print(f'Tasks: {total}  |  Workers: {WORKERS}  |  Model: FLUX.1-schnell')

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
            status = 'OK' if result.endswith('KB') else 'FAIL'
            print(f'[{done:3}/{total}] {status} {desc[:50]:<50} {result:<14} {dur:.1f}s  ETA {eta//60}m{eta%60:02d}s')
            if result.endswith('KB'):
                ok += 1
            else:
                failed.append((desc, result))

    total_time = int(time.time() - t0)
    print(f'\nDone {ok}/{total}  in {total_time//60}m{total_time%60:02d}s')
    if failed:
        print(f'FAILED {len(failed)}:')
        for d, m in failed[:20]:
            print(f'  {d}: {m}')


if __name__ == '__main__':
    main()
