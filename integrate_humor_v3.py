"""
Part Б + finish part А UI. One pass:

1. Build full deterministic labels for 4 categories:
   furniture/lighting/materials  ← taxonomy_v3.json (canonical ru, style-independent)
   humor                          ← humor_taxonomy.json (mechanism ru)
2. Patch index.html: ROOM_LABELS, ROOM_OPTS, CATS, cat-tabs, cat-panes,
   defaultSetup, progress counters 27 → 36.
3. Patch bot.py: CATS, CAT_RU, SLOT_LABELS, SLOT_OPTS (rebuilt for 4 cats).
4. Sync /tmp/catalog.json + catalog_v3.json with the humor category from
   prompts_humor.json (keys opt-{style}-humor-{room}-{idx}-{var}).

Idempotent: re-running replaces the same blocks.
"""
import json, re, shutil, sys, time
from pathlib import Path

REPO = Path(__file__).parent
TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
HUM = json.loads((REPO / 'humor_taxonomy.json').read_text(encoding='utf-8'))
PH = json.loads((REPO / 'prompts_humor.json').read_text(encoding='utf-8'))

S = ['japandi', 'modern_classic', 'scandi']
RM = ['living', 'bedroom', 'bathroom', 'kitchen']
C = ['furniture', 'lighting', 'materials', 'humor']
CAT_RU = {'furniture': 'Мебель', 'lighting': 'Освещение',
          'materials': 'Материалы', 'humor': 'Юмор'}
PX = {'furniture': 'f', 'lighting': 'l', 'materials': 'm', 'humor': 'h'}


def slot_ru(room, cat, idx):
    if cat == 'humor':
        return HUM[room][idx]['ru']
    return TAX[room][cat][idx]['ru']


# ---- shared label structures (ru identical across styles: v3 design) ----
labels = {s: {r: {c: [slot_ru(r, c, i) for i in range(9)] for c in C}
              for r in RM} for s in S}
opts = {s: {r: {c: [[slot_ru(r, c, i)] * 3 for i in range(9)] for c in C}
            for r in RM} for s in S}

# ============================ index.html ============================
HTML = REPO / 'index.html'
html = HTML.read_text(encoding='utf-8')
shutil.copy(HTML, f'/tmp/index.html.preB.{int(time.time())}')

_cj = dict(ensure_ascii=False, separators=(',', ':'))
new_room_labels = 'var ROOM_LABELS = ' + json.dumps(labels, **_cj) + ';'
new_room_opts = 'var ROOM_OPTS = ' + json.dumps(opts, **_cj) + ';'

# Anchored to the NEXT block so re-runs (single- or multi-line) can never
# over-match and swallow following code. Each pattern consumes its block +
# trailing newline; replacement restores one clean trailing newline.
def sub1(pattern, repl, s, what):
    s2, n = re.subn(pattern, lambda m: repl, s, count=1, flags=re.DOTALL)
    if n != 1:
        raise SystemExit(f'integrate: pattern for {what} matched {n}x (expected 1)')
    return s2

html = sub1(r'var ROOM_LABELS = \{.*?\n\};\n(?=var ROOM_OPTS)',
            new_room_labels + '\n', html, 'ROOM_LABELS')
html = sub1(r'var ROOM_OPTS = \{.*?\n\};\n(?=\nvar STYLE_FNAME)',
            new_room_opts + '\n', html, 'ROOM_OPTS')

# CATS array (anchored before ROOM_LABELS)
new_cats = ('var CATS = [\n'
            "  { id:'furniture', px:'f' },\n"
            "  { id:'lighting',  px:'l' },\n"
            "  { id:'materials', px:'m' },\n"
            "  { id:'humor',     px:'h' },\n"
            '];')
html = sub1(r'var CATS = \[.*?\n\];\n(?=var ROOM_LABELS)',
            new_cats + '\n', html, 'CATS')

# cat-tabs: add humor button (idempotent)
if 'data-cat="humor"' not in html:
    html = html.replace(
        '    <button class="cat-tab"    type="button" data-cat="materials" data-eid="tab-materials">Материалы</button>\n  </div>',
        '    <button class="cat-tab"    type="button" data-cat="materials" data-eid="tab-materials">Материалы</button>\n'
        '    <button class="cat-tab"    type="button" data-cat="humor"     data-eid="tab-humor">Юмор</button>\n  </div>')
    html = html.replace(
        '  <div class="cat-pane"    id="pane-materials" data-cat="materials"></div>',
        '  <div class="cat-pane"    id="pane-materials" data-cat="materials"></div>\n'
        '  <div class="cat-pane"    id="pane-humor"     data-cat="humor"></div>')

# defaultSetup object
html = html.replace(
    'var s = { furniture:{}, lighting:{}, materials:{} };',
    'var s = { furniture:{}, lighting:{}, materials:{}, humor:{} };')

# sequential cat-advance order must include humor
html = html.replace(
    "var order = ['furniture', 'lighting', 'materials'];",
    "var order = ['furniture', 'lighting', 'materials', 'humor'];")

# progress 27 -> 36
html = html.replace('<div class="prog" id="prog">27 / 27</div>',
                    '<div class="prog" id="prog">36 / 36</div>')
html = html.replace("el.textContent = n + ' / 27';",
                    "el.textContent = n + ' / 36';")
html = html.replace("el.className = 'prog' + (n === 27 ? ' done' : '');",
                    "el.className = 'prog' + (n === 36 ? ' done' : '');")

HTML.write_text(html, encoding='utf-8')
assert 'data-cat="humor"' in html and "id:'humor'" in html
assert "n + ' / 36'" in html and 'n === 36' in html
assert "'materials', 'humor']" in html, 'order array not patched'

# ============================== bot.py ==============================
BOT = REPO / 'bot.py'
src = BOT.read_text(encoding='utf-8')
shutil.copy(BOT, f'/tmp/bot.py.preB.{int(time.time())}')

new_cats_b = ("CATS     = [('furniture', 'f'), ('lighting', 'l'), "
              "('materials', 'm'), ('humor', 'h')]")
src = re.sub(r"CATS     = \[\('furniture'.*?\]", lambda m: new_cats_b,
             src, count=1)
new_catru = ("CAT_RU   = " + json.dumps(CAT_RU, ensure_ascii=False))
src = re.sub(r"CAT_RU   = \{[^}]*\}", lambda m: new_catru, src, count=1)

new_sl = 'SLOT_LABELS = ' + json.dumps(labels, ensure_ascii=False, indent=4)
new_so = 'SLOT_OPTS = ' + json.dumps(opts, ensure_ascii=False, indent=4)
src = re.sub(r'SLOT_LABELS = \{.*?\n\}', lambda m: new_sl, src, count=1,
             flags=re.DOTALL)
src = re.sub(r'SLOT_OPTS = \{.*?\n\}', lambda m: new_so, src, count=1,
             flags=re.DOTALL)

BOT.write_text(src, encoding='utf-8')
import ast
ast.parse(src)
assert "('humor', 'h')" in src and '"humor"' in src

# ============================ catalog ============================
CAT = '/tmp/catalog.json'
suffix = {'main': '', 'alt': ' (вар. 2)', 'alt2': ' (вар. 3)'}
if Path(CAT).exists():
    shutil.copy(CAT, f'{CAT}.preB.{int(time.time())}')
    cat = json.loads(Path(CAT).read_text(encoding='utf-8'))
else:
    cat = json.loads((REPO / 'catalog_v3.json').read_text(encoding='utf-8'))
items = cat['items']
upd = 0
for s in S:
    for r in RM:
        items[s][r].setdefault('humor', {})
        for idx in range(9):
            ru = HUM[r][idx]['ru']
            for var in ('main', 'alt', 'alt2'):
                k = f'opt-{s}-humor-{r}-{idx}-{var}'
                if k not in PH:
                    continue
                items[s][r]['humor'][f'{idx+1}_{var}'] = {
                    'name_ru': ru + suffix[var], 'positive': PH[k]}
                upd += 1
out = json.dumps(cat, ensure_ascii=False, indent=2)
if Path(CAT).exists() or True:
    Path(CAT).write_text(out, encoding='utf-8')
shutil.copy(CAT, str(REPO / 'catalog_v3.json'))

print(f'index.html + bot.py patched (4 cats incl. humor); '
      f'catalog humor entries: {upd}/324')
