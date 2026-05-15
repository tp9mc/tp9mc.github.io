"""
Rebuild ROOM_OPTS in index.html from labels.json (triples [main,alt,alt2]).
Replaces the existing `var ROOM_OPTS = { ... };` block in place.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).parent
HTML = REPO / 'index.html'
labels = json.loads((REPO / 'labels.json').read_text(encoding='utf-8'))

STYLES = ['japandi', 'modern_classic', 'scandi']
ROOMS = ['living', 'bedroom', 'bathroom', 'kitchen']
CATS = ['furniture', 'lighting', 'materials']


def js(s: str) -> str:
    return s.replace('\\', '\\\\').replace("'", "\\'")


lines = ['var ROOM_OPTS = {']
for si, s in enumerate(STYLES):
    lines.append(f'  {s}: {{')
    for ri, r in enumerate(ROOMS):
        lines.append(f'    {r}: {{')
        for ci, c in enumerate(CATS):
            triples = labels[s][r][c]
            arr = ','.join(
                '[' + ','.join(f"'{js(x)}'" for x in row) + ']'
                for row in triples
            )
            comma = ',' if ci < len(CATS) - 1 else ''
            lines.append(f'      {c}: [{arr}]{comma}')
        lines.append('    }' + (',' if ri < len(ROOMS) - 1 else ''))
    lines.append('  }' + (',' if si < len(STYLES) - 1 else ''))
lines.append('};')
new_block = '\n'.join(lines)

html = HTML.read_text(encoding='utf-8')
# replace from 'var ROOM_OPTS = {' up to the matching top-level '};'
pat = re.compile(r'var ROOM_OPTS = \{.*?\n\};', re.DOTALL)
m = pat.search(html)
if not m:
    raise SystemExit('ROOM_OPTS block not found')
html2 = html[:m.start()] + new_block + html[m.end():]
HTML.write_text(html2, encoding='utf-8')

# sanity: count triples
n = sum(len(labels[s][r][c]) for s in STYLES for r in ROOMS for c in CATS)
print(f'ROOM_OPTS rebuilt: {n} slot-rows, block {len(new_block)} chars')
print(f'old block was {m.end()-m.start()} chars')
