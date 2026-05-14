# Отчёт о ночной работе — 2026-05-14

**Старт:** 00:55  **Финиш:** ~10:55  **Все правки live в `main`.**

---

## TL;DR — что заехало в production

| # | Что | Файл / ссылка |
|---|---|---|
| 1 | Чистка `prompts.json` — 162 проблемы починены | `prompts.json` |
| 2 | Style Palette (12 ячеек) | `palette.json` |
| 3 | Граф совместимости (21 правило) | `compat_graph.json` |
| 4 | LLM-переписаны все 648 промтов, единая модель | `prompts_v2.json` |
| 5 | Регенерированы 648 иконок через FLUX.1-schnell | `assets/items/` |
| 6 | Регенерированы 15 style+room preview-картинок | `assets/styles/`, `assets/rooms/` |
| 7 | **alt2 (3-й вариант)**: +324 промта + 324 иконки | `prompts_alt2.json` |
| 8 | UI: 3-колоночный picker для main/alt/alt2 | `index.html` |
| 9 | catalog обновлён для alt2 (bot scene-build умеет alt2) | `catalog_v2.json` |
| 10 | Бот: команды `/versions` `/changelog` `/rollback` | `bot.py` |

12 пар BEFORE/AFTER для глаз: [preview_samples/index.html](preview_samples/index.html)

---

## Что нового в боте

| Команда | Что делает |
|---|---|
| `/versions` | Последние 10 коммитов: hash, дата, описание. Только для редакторов. |
| `/changelog` | Шлёт `CHANGELOG.md` (как текст или документ если больше 3.8KB). |
| `/rollback` | Inline-меню с 5 вариантами отката. Подтверждение → `git revert` (история сохраняется, не reset) → push → автоперезапуск бота. `site_edits.json` НЕ откатывается. |

Доступ к `/versions` и `/rollback` — только для chat_id из `EDITOR_CHAT_IDS` (ты + Саша).

---

## Какие промты получились (примеры)

**modern_classic/living/furniture/0/alt2** (новый 3-й вариант):
> 3D render of a low-slung modern velvet sofa, deep emerald green plush pile, polished walnut base with brass trim line, ivory raw silk accent pillows, symmetric proportions, isolated on pure white background, soft studio lighting, ambient occlusion, centered front view, professional product photography, 8k.

**japandi/bedroom/lighting/0/main** (rewrite v2):
> 3D render of a bedside table lamp with light natural oak base oil finish, matte black steel arm, ivory wool chunky knit shade, warm dim light, subtle natural texture, isolated on pure white background, soft studio lighting, ambient occlusion, centered front view, professional product photography, 8k.

Длины: min 171, max 496, среднее 318 chars. Русского нет, у всех корректный quality-tail.

---

## Где что лежит (полный реестр)

| Файл | Назначение |
|---|---|
| `prompts.json` (972 entries) | Активные промты: 648 main+alt (v2) + 324 alt2 |
| `prompts_v2.json` | Исходник для main+alt rewrite |
| `prompts_alt2.json` | Исходник для alt2 |
| `prompts_v1_for_compare.json` | Снэпшот v1 для сравнения |
| `palette.json` | Палитра 12 ячеек (style × room) |
| `compat_graph.json` | 21 правило совместимости |
| `catalog_v2.json` | Catalog с alt2 (snapshot, оригинал в `/tmp/catalog.json`) |
| `rewrite_prompts.py` | LLM-rewriter для main/alt |
| `rewrite_alt2.py` | LLM-rewriter для alt2 (с FORM-only differentiation) |
| `regen_items_from_v2.py PROMPTS_FILE` | Регенерация иконок из любого prompts-файла |
| `regen_previews.py` | Регенерация style/room previews |
| `preview_samples/` | 12 BEFORE/AFTER пар для визуальной проверки |
| `send_bot_report.py` | Шлёт отчёт в Telegram |

---

## Бэкапы (откат вручную, если что)

| Что | Где | Когда |
|---|---|---|
| `prompts.json` до v2 | `backups/tp9mc/prompts-before-v2-activation-20260514_024352.json` | 02:44 |
| `prompts.json` до alt2 merge | `backups/tp9mc/prompts-before-alt2-*.json` | 10:50 |
| `assets/items/` до regen | `backups/tp9mc/assets-items-backup-20260514_012548.tar.gz` | 01:25 |
| `assets/styles/` + `rooms/` до regen | `backups/tp9mc/assets-previews-backup-20260514_102809.tar.gz` | 10:28 |

Но проще: открой `/rollback` в боте и выбери коммит.

---

## Что НЕ сделал (и почему)

| Не сделано | Причина |
|---|---|
| Категория `textiles` (216 промтов + UI вкладка) | Большая поверхность UI + дизайн-решения по 108 русским подписям слотов. Хочу твою валидацию палитры/alt2 сначала. |
| Категория `decor` | То же. |
| Категория `tech` | То же + асимметрия (только living/bedroom/kitchen). |
| Переезд на FLUX.1-Krea / FLUX.2 | Платный provider (fal.ai/replicate). Бесплатных альтернатив schnell на HF не осталось. |
| Регистрация в `set_my_commands` для /publish, /test | Не было запроса. |

---

## Затраты

| Ресурс | Использовано |
|---|---|
| HF balance ($16 был) | $0 — FLUX.1-schnell на hf-inference бесплатен |
| OpenRouter | ~$0.03 от пробного баланса (5 ячеек на Sonnet 4.6 в начале) + $0 (остальные 67 ячеек на openrouter/free) |
| Время бота — даунтайма | ~30 сек × 3 рестарта (versioning, alt2 catalog, get_item patch) |

---

## Что от тебя нужно

1. **Открой Mini App** — посмотри новый picker с 3-мя вариантами, проверь иконки alt2.
2. **Открой `/preview_samples/index.html`** — 12 пар BEFORE/AFTER ключевых слотов.
3. **Скажи в боте** что заходит / не заходит. Если конкретная ячейка плохая — я перегенерю.
4. **Реши** что делать дальше:
   - Добавлять `textiles` / `decor` / `tech` категории?
   - Перейти на платный fal.ai с FLUX.2?
   - Что-то поправить в палитре (`palette.json`)?

Команды для управления:
- `/versions` — посмотреть, что задеплоено
- `/rollback` — откатиться к любой точке
- `/changelog` — журнал изменений
