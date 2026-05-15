"""
Humor (4th category) prompt generator. 3 styles x 4 rooms x 9 comedic
mechanisms x 3 variants = 324 prompts. Mechanism is fixed by
humor_taxonomy.json; STYLE only changes the finish (palette.json) so the
absurd object is executed in the room's expensive-minimalist materials —
that contrast is the extra comedy layer.

alt/alt2 = a DIFFERENT gag of the same mechanism (new joke, not a material
swap), so the user has real choices.

Output: prompts_humor.json  keys opt-{style}-humor-{room}-{idx}-{variant}
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import OPENROUTER_KEY  # noqa

TAX = json.loads((REPO / 'humor_taxonomy.json').read_text(encoding='utf-8'))
PALETTE = json.loads((REPO / 'palette.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
MODEL = 'openrouter/free'
OR = 'https://openrouter.ai/api/v1/chat/completions'
TAIL = ('isolated on pure white background, soft studio lighting, ambient '
        'occlusion, centered front view, professional product photography, 8k.')


def build_req(style, room):
    pal = PALETTE[style][room]
    dna = PALETTE[style]['_dna']
    slots = TAX[room]
    spec = {}
    for idx, s in enumerate(slots):
        spec[idx] = {'mechanism': s['mech'], 'gag': s['en'], 'ru': s['ru']}
    return f"""Generate FLUX.1-schnell text-to-image prompts for a HUMOR / joke
furniture category in a {style} {room}. Each item is an absurd comedic object
that still belongs in the room and is built in the room's refined materials —
the deadpan "expensive minimalism doing something stupid" contrast IS the joke.

Style: {style} — {dna}
Palette (execute the gag ONLY in these materials/colors):
{json.dumps(pal, ensure_ascii=False, indent=2)}

SLOT SPEC — for each idx the comedic MECHANISM is fixed; render the gag as
an over-the-top, unmistakable visual joke (FLUX.1-schnell flattens subtle
humor, so make the gag exaggerated and obvious):
{json.dumps(spec, ensure_ascii=False, indent=2)}

For every idx produce THREE variants:
- main : the gag exactly as described, in primary palette materials
- alt  : a DIFFERENT gag of the SAME mechanism (new joke), secondary materials
- alt2 : a third DIFFERENT gag of the same mechanism, distinct silhouette

Rules: English only; 200-380 chars; start "3D render of a/an [subject]";
the comedic object must read clearly; keep it a single hero object (no people,
no text, no logos); execute strictly in the {style} {room} palette above;
end EVERY prompt with: "{TAIL}"

OUTPUT strict JSON, keys EXACTLY like "opt-{style}-humor-{room}-<idx>-<variant>",
27 entries (idx 0-8 x main/alt/alt2). No markdown/prose."""


def call(text):
    r = requests.post(OR, headers={'Authorization': f'Bearer {OPENROUTER_KEY}',
                                   'Content-Type': 'application/json'},
                       json={'model': MODEL, 'messages': [{'role': 'user', 'content': text}],
                             'temperature': 0.7, 'max_tokens': 3500}, timeout=180)
    r.raise_for_status()
    c = r.json()['choices'][0]['message']['content'].strip()
    if c.startswith('```'):
        c = c.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(c)


def proc(style, room):
    req = build_req(style, room)
    last = None
    for a in range(5):
        try:
            res = call(req)
            if len(res) >= 22:
                return style, room, res, None
            last = f'partial {len(res)}'
        except Exception as e:
            last = repr(e)[:140]
        time.sleep(8 * (a + 1))
    return style, room, {}, last


def main():
    out = REPO / 'prompts_humor.json'
    acc = json.loads(out.read_text()) if out.exists() else {}
    cells = [(s, r) for s in STYLES for r in ROOMS
             if sum(1 for k in acc if k.startswith(f'opt-{s}-humor-{r}-')) < 22]
    print(f'cells to do: {len(cells)} (acc {len(acc)})')
    errs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(proc, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            s, r, res, err = f.result()
            tag = f'{s}/{r}'
            if err:
                errs.append((tag, err)); print(f'[{i}/{len(cells)}] {tag} ERR {err}')
            else:
                acc.update(res); print(f'[{i}/{len(cells)}] {tag} +{len(res)}')
                out.write_text(json.dumps(acc, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'done. total {len(acc)} errs {len(errs)}')
    for e in errs:
        print(' ', e)


if __name__ == '__main__':
    main()
