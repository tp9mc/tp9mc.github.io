"""
Deterministic v3 labels: each slot's label = its canonical RU name from
taxonomy_v3.json (subject is now fixed by taxonomy, so the label can never
mismatch the image). All 3 variants share the slot's RU noun; the thumbnails
differentiate main/alt/alt2 visually.

Writes labels.json (ROOM_OPTS triples) ready for patch_room_opts.py.
"""
import json
from pathlib import Path

REPO = Path(__file__).parent
TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS = ['furniture', 'lighting', 'materials']

out = {}
for s in STYLES:
    out[s] = {}
    for r in ROOMS:
        out[s][r] = {}
        for c in CATS:
            slots = TAX[r][c]
            out[s][r][c] = [[sl['ru'], sl['ru'], sl['ru']] for sl in slots]

(REPO / 'labels.json').write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
n = sum(len(out[s][r][c]) for s in STYLES for r in ROOMS for c in CATS)
print(f'labels.json (v3, deterministic) written: {n} slot-rows')
print('sample living/furniture:', out['japandi']['living']['furniture'])
