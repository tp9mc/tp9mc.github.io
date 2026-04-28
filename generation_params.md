# Параметры генерации assets — FLUX.1-schnell

Модель: `black-forest-labs/FLUX.1-schnell`  
Endpoint: `https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell`  
Скрипт: `generate_assets_hf.py`  
Формат: WebP, quality=85, method=4  
Воркеры: 3 параллельных потока  

---

## 1. Style previews — 3 файла

**Путь:** `assets/styles/{style}.webp`  
**Генерация:** 1152×640 → **720×400** WebP  

| Файл | Промпт |
|------|--------|
| `japandi.webp` | japandi living room interior, low oak coffee table, linen sofa, rice paper pendant lamp, zen atmosphere, natural light, professional interior photography, architectural digest |
| `modern-classic.webp` | modern classic living room interior, marble floors, tufted velvet sofa, brass chandelier, grand symmetrical space, professional interior photography, luxurious |
| `scandi.webp` | scandinavian minimalist living room interior, light oak floor, white walls, cozy wool throws, natural daylight, professional interior photography, hygge |

---

## 2. Room previews — 12 файлов

**Путь:** `assets/rooms/{style}__{room}.webp`  
**Генерация:** 1152×512 → **720×320** WebP  
**Суффикс промпта:** `professional interior photography, natural light, high quality, wide angle`

### Style DNA (добавляется в начало)

| Стиль | DNA |
|-------|-----|
| japandi | japandi style, warm oak wood, natural linen, zen minimalism, earth tones, wabi-sabi |
| modern_classic | modern classic style, Carrara marble, deep velvet, polished brass, grand symmetry |
| scandi | scandinavian style, light birch wood, crisp white walls, soft wool textiles, hygge |

### Room DNA (добавляется после style DNA)

| Комната | DNA |
|---------|-----|
| living | living room, comfortable seating, coffee table, ambient lighting |
| bedroom | bedroom, bed with pillows, bedside tables, soft warm lighting |
| bathroom | bathroom, clean white fixtures, natural stone, elegant vanity |
| kitchen | kitchen, functional workspace, clean countertops, organized storage |

**Итого 12 файлов:** japandi/modern_classic/scandi × living/bedroom/bathroom/kitchen

---

## 3. Pair-picker icons — 648 файлов

**Путь:** `assets/items/{style}/{room}/{category}/{slot}__{variant}.webp`  
**Генерация:** 1024×1024 → **400×400** WebP

### Комнаты living / bedroom / bathroom (3 стиля × 3 комнаты × 3 кат. × 9 слотов × 2 варианта)

Промпты берутся из `catalog.json`:  
`catalog['items'][style][room][category]['{slot}_{variant}']['positive']`

- **Стили:** japandi, modern_classic, scandi  
- **Комнаты:** living, bedroom, bathroom  
- **Категории:** furniture (`f`), lighting (`l`), materials (`m`)  
- **Слоты:** 1–9  
- **Варианты:** main, alt  

Слот/вариант пропускается если в каталоге нет записи `{slot}_{variant}`.

### Комната kitchen (3 стиля × 1 комната × 3 кат. × 9 слотов × 2 варианта)

**Вариант `main`:** из `catalog['items'][style]['kitchen'][category]['{slot}']['positive']`  
**Вариант `alt`:** фиксированные промпты из `KITCHEN_ALTS` (см. ниже)

#### KITCHEN_ALTS — furniture (слоты 1–9)

| Слот | Промпт |
|------|--------|
| 1 | minimalist 3D render of a kitchen island, dark smoked oak base, thick white quartz countertop, built-in cooktop, integrated drawers, isolated on pure white background, soft studio lighting, high quality |
| 2 | minimalist 3D render of a bar stool, matte black steel frame, round solid oak seat, footrest ring, isolated on pure white background, soft studio lighting, high quality |
| 3 | minimalist 3D render of a round dining table, white lacquer top, tapered wooden legs, isolated on pure white background, soft studio lighting, high quality |
| 4 | minimalist 3D render of a wall-mounted kitchen cabinet, frosted glass door fronts, white matte frame, isolated on pure white background, soft studio lighting |
| 5 | minimalist 3D render of a tall kitchen pantry cabinet, smooth matte white fronts, push-to-open, thin shadow gap, isolated on pure white background, soft studio lighting |
| 6 | minimalist 3D render of a two-tier kitchen rolling cart, stainless steel frame, solid wood top shelf, wire bottom shelf, swivel casters, isolated on pure white background, soft studio lighting |
| 7 | minimalist 3D render of an undermount single-basin stainless steel kitchen sink, brushed finish, seamless countertop edge, isolated on pure white background, soft studio lighting |
| 8 | minimalist 3D render of a single-lever kitchen faucet, brushed nickel, high arc spout, pull-down spray, isolated on pure white background, soft studio lighting, product photography |
| 9 | minimalist 3D render of a rectangular white marble cutting board, grey veining, isolated on pure white background, soft studio lighting, product photography |

#### KITCHEN_ALTS — lighting (слоты 1–9)

| Слот | Промпт |
|------|--------|
| 1 | minimalist 3D render of an industrial matte black metal pendant lamp, exposed bulb, over kitchen island, isolated on pure white background, soft studio lighting |
| 2 | minimalist 3D render of under-cabinet LED strip lighting bar, slim aluminum profile, warm white glow, isolated on pure white background, product photography |
| 3 | minimalist 3D render of flush-mount recessed ceiling spotlights, white trim ring, isolated on pure white background, soft studio lighting |
| 4 | minimalist 3D render of a matte black adjustable ceiling spotlight, GU10 lamp, isolated on pure white background, soft studio lighting |
| 5 | minimalist 3D render of a magnetic LED track system, slim rail with three adjustable spots, isolated on pure white background, soft studio lighting |
| 6 | minimalist 3D render of a minimalist gooseneck wall-mounted lamp, matte white, isolated on pure white background, soft studio lighting |
| 7 | minimalist 3D render of a square flush LED ceiling panel, warm white, matte white frame, isolated on pure white background, soft studio lighting |
| 8 | minimalist 3D render of a capacitive touch smart light switch, minimalist white glass panel, isolated on pure white background, product photography |
| 9 | minimalist 3D render of a recessed LED plinth light strip, floor-level, soft warm glow line, isolated on pure white background, soft studio lighting |

#### KITCHEN_ALTS — materials (слоты 1–9)

| Слот | Промпт |
|------|--------|
| 1 | minimalist 3D render of a kitchen backsplash tile swatch, white zellige ceramic handmade tiles, irregular texture, isolated on pure white background, product photography |
| 2 | minimalist 3D render of a kitchen countertop slab, polished Calacatta marble, gold grey veining, isolated on pure white background, product photography |
| 3 | minimalist 3D render of kitchen cabinet door fronts, flat matte sage green lacquer, isolated on pure white background, product photography |
| 4 | minimalist 3D render of folded kitchen textiles, cotton waffle-weave dish towels, muted clay color, isolated on pure white background, soft studio lighting |
| 5 | minimalist 3D render of ceramic dinnerware, matte white plates and bowls stacked, isolated on pure white background, soft studio lighting |
| 6 | minimalist 3D render of kitchen counter decor, small terracotta herb pot with rosemary and wooden spoon, isolated on pure white background, soft studio lighting |
| 7 | minimalist 3D render of modern kitchen cabinet bar handles, brushed brass, set of three, isolated on pure white background, product photography |
| 8 | minimalist 3D render of kitchen floor tile swatch, large format matte concrete-look porcelain, isolated on pure white background, product photography |
| 9 | minimalist 3D render of a linen roller blind, light-filtering, natural ecru, isolated on pure white background, soft studio lighting |

---

## Итого

| Тип | Кол-во | Размер | Путь |
|-----|--------|--------|------|
| Style previews | 3 | 720×400 | assets/styles/ |
| Room previews | 12 | 720×320 | assets/rooms/ |
| Pair-picker icons (living/bedroom/bathroom) | ~540 | 400×400 | assets/items/ |
| Pair-picker icons (kitchen) | ~108 | 400×400 | assets/items/ |
| **Итого** | **~663** | | |
