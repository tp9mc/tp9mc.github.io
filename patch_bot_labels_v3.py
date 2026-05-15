"""
Replace bot.py SLOT_LABELS and SLOT_OPTS blocks with deterministic v3
structures derived from taxonomy_v3.json. Also make build_report's
variant index handle alt2.
"""
import json, re
from pathlib import Path

REPO = Path(__file__).parent
TAX = json.loads((REPO / 'taxonomy_v3.json').read_text(encoding='utf-8'))
BOT = REPO / 'bot.py'
S = ['japandi', 'modern_classic', 'scandi']
RM = ['living', 'bedroom', 'bathroom', 'kitchen']
C = ['furniture', 'lighting', 'materials']


def py_lit(obj, ind=0):
    return json.dumps(obj, ensure_ascii=False).replace('": ', '": ')


labels = {s: {r: {c: [sl['ru'] for sl in TAX[r][c]] for c in C} for r in RM} for s in S}
opts = {s: {r: {c: [[sl['ru'], sl['ru'], sl['ru']] for sl in TAX[r][c]] for c in C}
            for r in RM} for s in S}

src = BOT.read_text(encoding='utf-8')

new_labels = 'SLOT_LABELS = ' + json.dumps(labels, ensure_ascii=False, indent=4)
new_opts = 'SLOT_OPTS = ' + json.dumps(opts, ensure_ascii=False, indent=4)

src2 = re.sub(r'SLOT_LABELS = \{.*?\n\}', new_labels, src, count=1, flags=re.DOTALL)
src2 = re.sub(r'SLOT_OPTS = \{.*?\n\}', new_opts, src2, count=1, flags=re.DOTALL)
# variant index: support alt2
src2 = src2.replace(
    "vi       = 0 if variant == 'main' else 1",
    "vi       = {'main': 0, 'alt': 1, 'alt2': 2}.get(variant, 0)")
src2 = src2.replace(
    "var_name = slot_opts[n - 1][vi] if n - 1 < len(slot_opts) else ('A' if vi == 0 else 'Б')",
    "var_name = slot_opts[n - 1][vi] if (n - 1 < len(slot_opts) and vi < len(slot_opts[n - 1])) else ['A', 'Б', 'В'][vi]")

BOT.write_text(src2, encoding='utf-8')
import ast
ast.parse(src2)
print('bot.py SLOT_LABELS/SLOT_OPTS replaced (v3), syntax OK')
