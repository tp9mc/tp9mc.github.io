"""Общие утилиты пайплайна: детерминированный рандом, I/O, нормализация текста."""
import hashlib
import json
import os
import random
import re
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DATA_DIR = os.path.join(ROOT, "data")
SHOPS_DIR = os.path.join(ROOT, "shops")


def rng_for(*keys) -> random.Random:
    """Детерминированный Random, засеянный от произвольного набора ключей.

    Один и тот же набор ключей всегда даёт одну и ту же последовательность —
    так рынок «живёт» во времени, но любой запуск воспроизводим.
    """
    seed = hashlib.sha256("|".join(str(k) for k in keys).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj, compact=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        f.write("\n")


def append_history(path, entry, max_len=800):
    hist = load_json(path, default=[])
    hist.append(entry)
    save_json(path, hist[-max_len:], compact=True)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\wа-яё%/.-]+", re.IGNORECASE)

# частые транслитерации брендов и слов в фидах конкурентов
TRANSLIT = {
    "найк": "nike", "адидас": "adidas", "пума": "puma", "рибок": "reebok",
    "ливайс": "levis", "levi's": "levis", "гесс": "guess", "манго": "mango",
    "твое": "tvoe", "твоё": "tvoe", "остин": "ostin", "заря": "zarina",
    "кроссовки": "кроссовки", "джинсы": "джинсы",
}


def norm_text(s: str) -> str:
    if not s:
        return ""
    s = s.lower().replace("ё", "е")
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    toks = [TRANSLIT.get(t, t) for t in s.split(" ")]
    return " ".join(toks)


def tokens(s: str):
    return set(t for t in norm_text(s).split(" ") if len(t) > 1)


def trigrams(s: str):
    s = norm_text(s).replace(" ", "_")
    return set(s[i:i + 3] for i in range(max(0, len(s) - 2)))
