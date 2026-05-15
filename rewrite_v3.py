"""
v3 prompt generator. Rewrites ALL 972 prompts so each slot matches its NEW
v3 canonical subject (taxonomy_v3.json), constrained by palette + compat
graph. Single model (openrouter/free) for consistency.

Output: prompts_v3.json  (keys opt-{style}-{room}-{cat}-{idx}-{variant})
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import OPENROUTER_KEY  # noqa

TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
PALETTE = json.loads((REPO / 'palette.json').read_text(encoding='utf-8'))
GRAPH = json.loads((REPO / 'compat_graph.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS = ['furniture', 'lighting', 'materials']
MODEL = 'openrouter/free'
OR = 'https://openrouter.ai/api/v1/chat/completions'
TAIL = ('isolated on pure white background, soft studio lighting, ambient '
        'occlusion, centered front view, professional product photography, 8k.')


def rules(style, room, cat):
    return [{'id': r['id'], 'rule': r['rule']} for r in GRAPH['rules']
            if r['scope'] in ('all', style, room) and cat in r.get('applies_to', [])]


def build_req(style, room, cat):
    pal = PALETTE[style][room]
    dna = PALETTE[style]['_dna']
    slots = TAX[room][cat]
    spec = {}
    for idx, s in enumerate(slots):
        spec[idx] = {'subject': s['en'], 'role': s['layer'], 'ru': s['ru']}
    return f"""Generate FLUX.1-schnell text-to-image prompts for {cat} in a {style} {room}.

Style: {style} — {dna}
Palette (use ONLY these materials/colors):
{json.dumps(pal, ensure_ascii=False, indent=2)}

Compatibility rules (obey all):
{json.dumps(rules(style, room, cat), ensure_ascii=False, indent=2)}

SLOT SPEC — for each idx produce the EXACT subject (this is a fixed
functional taxonomy, do not substitute the object):
{json.dumps(spec, ensure_ascii=False, indent=2)}

For every idx produce THREE variants:
- main : subject in primary palette materials (wood_primary, fabric_main...)
- alt  : subject in secondary palette materials (wood_secondary, fabric_accent...)
- alt2 : same subject + palette, DIFFERENT FORM (silhouette/proportion/config)

Rules: English only; 200-380 chars; start "3D render of a/an [subject]"
(or "minimalist 3D render of" for small swatches/samples); materials =
flat sample/swatch framing; end EVERY prompt with: "{TAIL}"
No people, no text, no logos.

OUTPUT strict JSON, keys EXACTLY like "opt-{style}-{room}-{cat}-<idx>-<variant>",
27 entries (idx 0-8 x main/alt/alt2). No markdown/prose."""


def call(text):
    r = requests.post(OR, headers={'Authorization': f'Bearer {OPENROUTER_KEY}',
                                   'Content-Type': 'application/json'},
                       json={'model': MODEL, 'messages': [{'role': 'user', 'content': text}],
                             'temperature': 0.4, 'max_tokens': 3500}, timeout=180)
    r.raise_for_status()
    c = r.json()['choices'][0]['message']['content'].strip()
    if c.startswith('```'):
        c = c.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(c)


def proc(style, room, cat):
    req = build_req(style, room, cat)
    last = None
    for a in range(5):
        try:
            res = call(req)
            if len(res) >= 22:
                return style, room, cat, res, None
            last = f'partial {len(res)}'
        except Exception as e:
            last = repr(e)[:140]
            time.sleep(8 * (a + 1))
    return style, room, cat, {}, last


def main():
    out = REPO / 'prompts_v3.json'
    acc = json.loads(out.read_text()) if out.exists() else {}
    cells = [(s, r, c) for s in STYLES for r in ROOMS for c in CATS
             if sum(1 for k in acc if k.startswith(f'opt-{s}-{r}-{c}-')) < 22]
    print(f'cells to do: {len(cells)} (acc {len(acc)})')
    errs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(proc, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            s, r, c, res, err = f.result()
            tag = f'{s}/{r}/{c}'
            if err:
                errs.append((tag, err)); print(f'[{i}/{len(cells)}] {tag} ERR {err}')
            else:
                acc.update(res); print(f'[{i}/{len(cells)}] {tag} +{len(res)}')
                out.write_text(json.dumps(acc, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'done. total {len(acc)} errs {len(errs)}')
    for e in errs: print(' ', e)


if __name__ == '__main__':
    main()
