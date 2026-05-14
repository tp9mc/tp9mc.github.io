"""
Generate alt2 (3rd variant) prompts for all 36 cells = 324 prompts.

Per compat_graph rule "alt2_variant_form_only", alt2 differs from main/alt by
FORM only (silhouette/dimension/configuration), uses SAME palette materials.

Same single model (openrouter/free) for consistency.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import OPENROUTER_KEY  # noqa: E402

PALETTE = json.loads((REPO / 'palette.json').read_text(encoding='utf-8'))
GRAPH = json.loads((REPO / 'compat_graph.json').read_text(encoding='utf-8'))
V2 = json.loads((REPO / 'prompts_v2.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATEGORIES = ['furniture', 'lighting', 'materials']

MODEL = 'openrouter/free'
OR_URL = 'https://openrouter.ai/api/v1/chat/completions'
QUALITY_TAIL = ('isolated on pure white background, soft studio lighting, '
                'ambient occlusion, centered front view, professional product photography, 8k.')


def cell_pairs(style, room, cat):
    """Returns dict: {slot_idx: {'main': prompt, 'alt': prompt}}"""
    out = {}
    for idx in range(9):
        m = V2.get(f'opt-{style}-{room}-{cat}-{idx}-main')
        a = V2.get(f'opt-{style}-{room}-{cat}-{idx}-alt')
        if m and a:
            out[idx] = {'main': m, 'alt': a}
    return out


def relevant_rules(style, room, cat):
    out = []
    for r in GRAPH['rules']:
        if r['scope'] in ('all', style, room) and cat in r.get('applies_to', []):
            out.append({'id': r['id'], 'rule': r['rule']})
    return out


def build_request(style, room, cat, pairs):
    palette_cell = PALETTE[style][room]
    rules = relevant_rules(style, room, cat)
    style_dna = PALETTE[style]['_dna']

    return f"""You are generating a THIRD VARIANT (alt2) of text-to-image prompts.

CONTEXT:
- Style: {style} — {style_dna}
- Room: {room}
- Category: {cat}

PALETTE (use ONLY these materials and colors):
{json.dumps(palette_cell, ensure_ascii=False, indent=2)}

KEY RULE: alt2 differs from main/alt by FORM ONLY (silhouette, dimension,
configuration), NOT by material. Same palette materials as main/alt, just a
different shape/arrangement. Think "the third design alternative the customer
considers when picking from this slot".

RULES:
{json.dumps(rules, ensure_ascii=False, indent=2)}

EXISTING main/alt PAIRS (for each slot, give me a NEW alt2 that complements them):
{json.dumps(pairs, ensure_ascii=False, indent=2)}

INSTRUCTIONS:
1. For each slot key (0-8), output ONE alt2 prompt.
2. Same subject type as main (sofa stays sofa).
3. Different FORM: try a different silhouette/configuration/proportion.
4. Materials: same palette. Lean toward MIX of wood_primary + secondary, or
   fabric_main + accent — give visual variety while staying palette-faithful.
5. Length 200-400 chars, English only.
6. End every prompt with: "{QUALITY_TAIL}"
7. Start every prompt with "3D render of a/an [subject]" (or "minimalist 3D render of" for small items).

OUTPUT (strict JSON, no markdown, no prose):
{{"opt-{style}-{room}-{cat}-0-alt2": "...", "opt-{style}-{room}-{cat}-1-alt2": "...", ...}}"""


def call_llm(prompt_text):
    r = requests.post(OR_URL,
        headers={'Authorization': f'Bearer {OPENROUTER_KEY}', 'Content-Type': 'application/json'},
        json={'model': MODEL, 'messages': [{'role': 'user', 'content': prompt_text}],
              'temperature': 0.5, 'max_tokens': 3000},
        timeout=180)
    r.raise_for_status()
    content = r.json()['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(content)


def process_cell(style, room, cat):
    pairs = cell_pairs(style, room, cat)
    if not pairs:
        return style, room, cat, {}, None
    req = build_request(style, room, cat, pairs)
    last_err = None
    for attempt in range(4):
        try:
            result = call_llm(req)
            if len(result) >= 7:
                return style, room, cat, result, None
            last_err = f'partial: {len(result)} keys'
        except Exception as e:
            last_err = repr(e)[:160]
            time.sleep(3 * (attempt + 1))
    return style, room, cat, {}, last_err


def main():
    out_path = REPO / 'prompts_alt2.json'
    cells = [(s, r, c) for s in STYLES for r in ROOMS for c in CATEGORIES]
    print(f'Total cells: {len(cells)}')

    new = {}
    errors = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(process_cell, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            s, r, c, result, err = f.result()
            tag = f'{s}/{r}/{c}'
            if err:
                errors.append((tag, err))
                print(f'[{i:2d}/{len(cells)}] {tag}  ERROR: {err}')
                continue
            new.update(result)
            print(f'[{i:2d}/{len(cells)}] {tag}  +{len(result)}')
            out_path.write_text(json.dumps(new, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'\nDone in {time.time()-t0:.1f}s. Total alt2 prompts: {len(new)}')
    if errors:
        print(f'Errors: {len(errors)}')
        for e in errors: print(f'  {e}')


if __name__ == '__main__':
    main()
