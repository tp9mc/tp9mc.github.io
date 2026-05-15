"""
Sync /tmp/catalog.json positives + name_ru from a prompts file + taxonomy_v3.
Usage: python3 sync_catalog.py prompts_v3.json
"""
import json, shutil, sys, time
from pathlib import Path

REPO = Path(__file__).parent
PF = REPO / (sys.argv[1] if len(sys.argv) > 1 else 'prompts_v3.json')
TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
P = json.loads(PF.read_text(encoding='utf-8'))
CAT = '/tmp/catalog.json'
S = ['japandi', 'modern_classic', 'scandi']
RM = ['living', 'bedroom', 'bathroom', 'kitchen']
C = ['furniture', 'lighting', 'materials']

shutil.copy(CAT, f'{CAT}.presync3.{int(time.time())}')
cat = json.loads(Path(CAT).read_text(encoding='utf-8'))
items = cat['items']
upd = 0
for s in S:
    for r in RM:
        for c in C:
            for idx in range(9):
                ru = TAX[r][c][idx]['ru']
                for var in ('main', 'alt', 'alt2'):
                    k = f'opt-{s}-{r}-{c}-{idx}-{var}'
                    if k not in P:
                        continue
                    suffix = {'main': '', 'alt': ' (вар. 2)', 'alt2': ' (вар. 3)'}[var]
                    items[s][r][c][f'{idx+1}_{var}'] = {
                        'name_ru': ru + suffix, 'positive': P[k]}
                    upd += 1
Path(CAT).write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding='utf-8')
shutil.copy(CAT, str(REPO / 'catalog_v3.json'))
print(f'catalog synced from {PF.name}: {upd} entries; repo copy catalog_v3.json')
