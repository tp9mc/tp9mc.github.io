"""
Generate concise Russian labels for every slot/variant from current prompts.json.
Output: labels.json = { "<style>": { "<room>": { "<cat>": [[m,a,a2], ...9] } } }
Single model (openrouter/free) for consistency.
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import OPENROUTER_KEY  # noqa: E402

P = json.loads((REPO / 'prompts.json').read_text(encoding='utf-8'))
STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS = ['furniture', 'lighting', 'materials']
MODEL = 'openrouter/free'
OR_URL = 'https://openrouter.ai/api/v1/chat/completions'


def cell_prompts(style, room, cat):
    out = {}
    for idx in range(9):
        for var in ('main', 'alt', 'alt2'):
            k = f'opt-{style}-{room}-{cat}-{idx}-{var}'
            if k in P:
                out[k] = P[k]
    return out


def build_request(style, room, cat, prompts):
    return f"""Below are text-to-image prompts for {cat} items in a {style} {room}.
For each key, return a SHORT Russian label (1-3 words, max 20 chars) naming the
object — what a user sees under the picture in a picker. Be concrete and
distinct between variants (main/alt/alt2 of same slot are different designs).

Examples: "Модульный диван", "Журнальный стол", "Торшер-дуга", "Дубовый паркет".
No generic words like "Вариант". Russian only. No quotes inside values.

PROMPTS:
{json.dumps(prompts, ensure_ascii=False, indent=2)}

OUTPUT strict JSON, same keys, value = Russian label string. No markdown/prose."""


def call_llm(text):
    r = requests.post(OR_URL,
        headers={'Authorization': f'Bearer {OPENROUTER_KEY}', 'Content-Type': 'application/json'},
        json={'model': MODEL, 'messages': [{'role': 'user', 'content': text}],
              'temperature': 0.3, 'max_tokens': 2000},
        timeout=180)
    r.raise_for_status()
    c = r.json()['choices'][0]['message']['content'].strip()
    if c.startswith('```'):
        c = c.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(c)


def process(style, room, cat):
    prompts = cell_prompts(style, room, cat)
    if not prompts:
        return style, room, cat, {}, 'empty'
    req = build_request(style, room, cat, prompts)
    last = None
    for attempt in range(4):
        try:
            res = call_llm(req)
            if len(res) >= len(prompts) * 0.8:
                return style, room, cat, res, None
            last = f'partial {len(res)}/{len(prompts)}'
        except Exception as e:
            last = repr(e)[:140]
            time.sleep(3 * (attempt + 1))
    return style, room, cat, {}, last


def main():
    raw = {}
    cells = [(s, r, c) for s in STYLES for r in ROOMS for c in CATS]
    errs = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(process, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            s, r, c, res, err = f.result()
            tag = f'{s}/{r}/{c}'
            if err:
                errs.append((tag, err))
                print(f'[{i:2d}/36] {tag} ERR {err}')
            else:
                raw.update(res)
                print(f'[{i:2d}/36] {tag} +{len(res)}')
            (REPO / 'labels_raw.json').write_text(
                json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')

    # restructure into ROOM_OPTS triples
    out = {}
    for s in STYLES:
        out[s] = {}
        for r in ROOMS:
            out[s][r] = {}
            for c in CATS:
                triples = []
                for idx in range(9):
                    row = []
                    for var in ('main', 'alt', 'alt2'):
                        k = f'opt-{s}-{r}-{c}-{idx}-{var}'
                        row.append(raw.get(k, ''))
                    triples.append(row)
                out[s][r][c] = triples
    (REPO / 'labels.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nDone {time.time()-t0:.0f}s. labels.json written. errors={len(errs)}')
    for e in errs:
        print(' ', e)


if __name__ == '__main__':
    main()
