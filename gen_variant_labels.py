"""
Short RU distinguishing captions per VARIANT (main/alt/alt2) so the user
can tell the 3 thumbnails apart and choose.

Source: prompts_v3.json (furniture/lighting/materials) + prompts_humor.json.
For each slot the 3 variants differ:
  v3   : main=primary materials, alt=secondary materials, alt2=different form
  humor: 3 genuinely different gags of the same comedic mechanism
So the caption must name what THAT thumbnail actually shows (material/finish
/ form / the concrete gag) in 2–4 RU words.

Output: variant_labels.json  {style:{room:{cat:[[c0,c1,c2] x9]}}}
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))
from bot_secrets import OPENROUTER_KEY  # noqa

PV = json.loads((REPO / 'prompts_v3.json').read_text(encoding='utf-8'))
PH = json.loads((REPO / 'prompts_humor.json').read_text(encoding='utf-8'))
TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
HUM = json.loads((REPO / 'humor_taxonomy.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS = ['furniture', 'lighting', 'materials', 'humor']
MODEL = 'openrouter/free'
OR = 'https://openrouter.ai/api/v1/chat/completions'


def key(style, room, cat, idx, var):
    if cat == 'humor':
        return f'opt-{style}-humor-{room}-{idx}-{var}'
    return f'opt-{style}-{room}-{cat}-{idx}-{var}'


def slot_ru(room, cat, idx):
    return (HUM[room][idx]['ru'] if cat == 'humor'
            else TAX[room][cat][idx]['ru'])


def build_req(style, room, cat):
    src = PH if cat == 'humor' else PV
    slots = {}
    for idx in range(9):
        trio = {}
        for var in ('main', 'alt', 'alt2'):
            k = key(style, room, cat, idx, var)
            if k in src:
                trio[var] = src[k]
        if trio:
            slots[idx] = {'ru': slot_ru(room, cat, idx), 'variants': trio}
    kind = ('three DIFFERENT comedic objects (different gags of the same '
            'joke mechanism)' if cat == 'humor'
            else 'the same object in three executions (main=primary '
                 'materials, alt=secondary materials, alt2=different form)')
    return f"""Ты пишешь КОРОТКИЕ русские подписи под миниатюрами в конструкторе
интерьера. Для каждого слота есть 3 варианта ({kind}). База слота уже
подписана сверху (напр. «Диван»), поэтому подпись варианта НЕ должна
повторять её — она должна объяснить, ЧЕМ ЭТОТ вариант визуально
отличается от двух других, чтобы пользователь мог выбрать.

Правила подписи:
- 2–4 слова, по-русски, с заглавной, без точки
- называй РАЗЛИЧАЮЩЕЕ: материал/отделку/форму (для мебели/света/материалов)
  или конкретный комичный объект (для юмора)
- три подписи одного слота должны быть РАЗНЫМИ и информативными
- НЕ повторяй название слота, не пиши «Вариант 1/2/3», без англ.

Слоты (idx → ru + промпты вариантов):
{json.dumps(slots, ensure_ascii=False, indent=1)}

Верни СТРОГО JSON: {{"<idx>": {{"main": "...", "alt": "...", "alt2": "..."}}}}
для всех idx. Без markdown и пояснений."""


def call(text):
    r = requests.post(OR, headers={'Authorization': f'Bearer {OPENROUTER_KEY}',
                                   'Content-Type': 'application/json'},
                       json={'model': MODEL,
                             'messages': [{'role': 'user', 'content': text}],
                             'temperature': 0.5, 'max_tokens': 2500},
                       timeout=180)
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
            if len(res) >= 8:
                return style, room, cat, res, None
            last = f'partial {len(res)}'
        except Exception as e:
            last = repr(e)[:140]
        time.sleep(8 * (a + 1))
    return style, room, cat, {}, last


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ''  # "style/room/cat" probe
    out = REPO / 'variant_labels.json'
    acc = json.loads(out.read_text()) if out.exists() else {}
    cells = []
    for s in STYLES:
        for r in ROOMS:
            for c in CATS:
                tag = f'{s}/{r}/{c}'
                if only and only != tag:
                    continue
                if acc.get(s, {}).get(r, {}).get(c):
                    continue
                cells.append((s, r, c))
    print(f'cells to do: {len(cells)}')
    errs = []
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(proc, *c): c for c in cells}
        for i, f in enumerate(as_completed(futs), 1):
            s, r, c, res, err = f.result()
            tag = f'{s}/{r}/{c}'
            if err:
                errs.append((tag, err))
                print(f'[{i}/{len(cells)}] {tag} ERR {err}')
                continue
            triples = []
            for idx in range(9):
                e = res.get(str(idx)) or res.get(idx) or {}
                triples.append([e.get('main', ''), e.get('alt', ''),
                                e.get('alt2', '')])
            acc.setdefault(s, {}).setdefault(r, {})[c] = triples
            out.write_text(json.dumps(acc, ensure_ascii=False, indent=2),
                           encoding='utf-8')
            print(f'[{i}/{len(cells)}] {tag} OK')
    print(f'done. errs {len(errs)}')
    for e in errs:
        print(' ', e)


if __name__ == '__main__':
    main()
