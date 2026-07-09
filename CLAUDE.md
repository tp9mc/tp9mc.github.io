# Repo notes for Claude

This is a GitHub Pages user site (`tp9mc.github.io`). The live site is served
from the `main` branch — changes are not visible to the user until they land
on `main`.

## Project

«Кабина пилота» Head of Product для двух продуктов: LT Parsing (краулер
конкурентов) и LT Matching (сопоставление товаров + ценовой индекс).
Пайплайн (`tools/run_pipeline.py`) запускается GitHub Actions каждые 3 часа:
симуляция рынка (`shops/`) → парсинг → мэтчинг → аналитика → коммит
`data/` + `shops/` в `main`. Дашборд — статический (`index.html`,
`assets/`), читает `data/*.json`. PO-боты для Telegram — `tools/po_bots/`
(секреты: PARSING_BOT_TOKEN, MATCHING_BOT_TOKEN). Подробности:
`docs/ARCHITECTURE.md`, `docs/METRICS.md`.

Локальная проверка: `python -m tools.run_pipeline` (один прогон),
затем `python -m http.server` и открыть дашборд.

Не редактируй руками содержимое `data/` и `shops/` — их пишет пайплайн.

## Deployment policy

- The user has authorized pushing changes directly to `main`.
- After completing a change on the assigned feature branch, fast-forward
  `main` to it and push `origin main` so the change goes live.
- No pull request required unless the user explicitly asks for one.
