"""
Stage 3: LLM-based prompt rewriter.

For each (style, room, category) cell (36 total), takes:
  - palette.json[style][room]
  - compat_graph.json (applicable rules)
  - current 18 prompts (9 slots × 2 variants: main, alt)

Produces 18 rewritten prompts that:
  - preserve the SUBJECT of each slot (ceiling stays ceiling)
  - use ONLY materials/colors from the cell's palette
  - obey compat graph rules
  - length 200–400 chars
  - English only
  - end with the standard quality tail

Output: prompts_v2.json (NOT yet activated — for review).
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bot_secrets import OPENROUTER_KEY  # noqa: E402

REPO = Path(__file__).parent
PROMPTS = json.loads((REPO / 'prompts.json').read_text(encoding='utf-8'))
PALETTE = json.loads((REPO / 'palette.json').read_text(encoding='utf-8'))
GRAPH = json.loads((REPO / 'compat_graph.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATEGORIES = ['furniture', 'lighting', 'materials']

MODEL = 'openrouter/free'
OR_URL = 'https://openrouter.ai/api/v1/chat/completions'

QUALITY_TAIL = (
    'isolated on pure white background, soft studio lighting, '
    'ambient occlusion, centered front view, professional product photography, 8k.'
)


def relevant_rules(style: str, room: str, cat: str) -> list:
    out = []
    for r in GRAPH['rules']:
        s_ok = r['scope'] in ('all', style, room)
        c_ok = cat in r.get('applies_to', [])
        if s_ok and c_ok:
            out.append({'id': r['id'], 'rule': r['rule']})
    return out


def cell_prompts(style: str, room: str, cat: str) -> dict:
    out = {}
    for idx in range(9):
        for var in ('main', 'alt'):
            k = f'opt-{style}-{room}-{cat}-{idx}-{var}'
            if k in PROMPTS:
                out[k] = PROMPTS[k]
    return out


def build_request(style: str, room: str, cat: str, prompts: dict) -> str:
    palette_cell = PALETTE[style][room]
    rules = relevant_rules(style, room, cat)
    style_dna = PALETTE[style]['_dna']

    msg = f"""You are rewriting text-to-image generation prompts for FLUX.1-schnell.

CONTEXT:
- Style: {style} — {style_dna}
- Room: {room}
- Category: {cat}

PALETTE (use ONLY these materials and colors; ignore anything else):
{json.dumps(palette_cell, ensure_ascii=False, indent=2)}

COMPATIBILITY RULES (must obey all):
{json.dumps(rules, ensure_ascii=False, indent=2)}

CURRENT PROMPTS TO REWRITE (preserve the SUBJECT/OBJECT in each — e.g. if it's a sofa, output a sofa; if it's a floor sample, output a floor sample):
{json.dumps(prompts, ensure_ascii=False, indent=2)}

INSTRUCTIONS:
1. For each key, output a rewritten English-only prompt for FLUX.1-schnell.
2. Length: 200–400 chars per prompt.
3. Identify the SUBJECT from the current text (e.g. "sofa", "pendant lamp", "floor swatch"). Keep it.
4. Replace materials/colors with ones from the palette. Strict — never invent new materials.
5. main variant uses primary materials (wood_primary, fabric_main, etc). alt variant uses secondary.
6. Every prompt MUST end with: "{QUALITY_TAIL}"
7. Begin every prompt with "3D render of a/an [subject]" (or "minimalist 3D render of" for very small items).
8. No people, no text, no logos.
9. If a current prompt is meaningless/empty, infer a reasonable subject from the slot number (slot 0 = ceiling/wall/big-surface, slot 8 = small accessory).

OUTPUT (strict JSON only, same keys as input):
{{"key1": "rewritten prompt...", "key2": "rewritten prompt...", ...}}

No prose, no markdown, no code fences. Just the JSON object."""
    return msg


def call_llm(prompt_text: str) -> dict:
    r = requests.post(
        OR_URL,
        headers={'Authorization': f'Bearer {OPENROUTER_KEY}',
                 'Content-Type': 'application/json'},
        json={
            'model': MODEL,
            'messages': [{'role': 'user', 'content': prompt_text}],
            'temperature': 0.4,
            'max_tokens': 4000,
        },
        timeout=120,
    )
    r.raise_for_status()
    body = r.json()
    content = body['choices'][0]['message']['content'].strip()
    if content.startswith('```'):
        content = content.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(content)


def process_cell(style: str, room: str, cat: str):
    prompts = cell_prompts(style, room, cat)
    if not prompts:
        return style, room, cat, {}, None
    req = build_request(style, room, cat, prompts)
    # retry up to 4 times for transient errors / rate limits on free tier
    last_err = None
    for attempt in range(4):
        try:
            result = call_llm(req)
            if len(result) >= 12:
                return style, room, cat, result, None
            last_err = f'partial: {len(result)} keys'
        except Exception as e:
            last_err = repr(e)[:160]
            time.sleep(3 * (attempt + 1))
    return style, room, cat, {}, last_err


def main():
    out_path = REPO / 'prompts_v2.json'
    log_path = Path('/tmp/propferma_audit/rewrite.log')
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # start fresh — single-model run requires consistent output
    if out_path.exists():
        out_path.unlink()

    cells = [(s, r, c) for s in STYLES for r in ROOMS for c in CATEGORIES]
    print(f'Total cells: {len(cells)}')

    new_prompts = {}
    errors = []
    t0 = time.time()
    # serialise to be friendly to free-tier rate limits; saves incrementally
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(process_cell, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            style, room, cat, result, err = f.result()
            tag = f'{style}/{room}/{cat}'
            if err:
                errors.append((tag, err))
                print(f'[{i:2d}/{len(cells)}] {tag}  ERROR: {err}')
                continue
            new_prompts.update(result)
            print(f'[{i:2d}/{len(cells)}] {tag}  +{len(result)}')
            # incremental save in case of late failure
            out_path.write_text(json.dumps(new_prompts, ensure_ascii=False, indent=2), encoding='utf-8')

    elapsed = time.time() - t0
    print(f'\nDone in {elapsed:.1f}s')
    print(f'  Total prompts rewritten: {len(new_prompts)}')
    print(f'  Errors: {len(errors)}')

    # validate
    import re
    bad = []
    for k, v in new_prompts.items():
        if not isinstance(v, str):
            bad.append((k, 'not str')); continue
        if re.search(r'[а-яА-ЯёЁ]', v):
            bad.append((k, 'has russian'))
        if len(v) < 150 or len(v) > 500:
            bad.append((k, f'length {len(v)}'))
        if not v.lower().endswith('8k.') and 'professional product photography' not in v.lower():
            bad.append((k, 'missing tail'))
    print(f'  Validation issues: {len(bad)}')
    for b in bad[:10]: print(f'    {b}')

    # write
    out_path.write_text(json.dumps(new_prompts, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nWrote {out_path}  ({len(new_prompts)} prompts)')

    log_path.write_text(json.dumps({
        'elapsed_s': elapsed,
        'cells_total': len(cells),
        'prompts_out': len(new_prompts),
        'errors': errors,
        'validation_issues': bad[:50],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
