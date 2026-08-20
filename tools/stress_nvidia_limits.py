"""Стресс-тест: реальные лимиты бесплатного тарифа NVIDIA по выбранным моделям.
Отвечает на вопрос: поможет ли новый API-ключ?
  - 429 Too Many Requests  -> лимит на КЛЮЧ, новый ключ поможет
  - 503 ResourceExhausted / 529 Overloaded -> общая нехватка мощностей, ключ НЕ поможет
"""

import collections
import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://integrate.api.nvidia.com/v1/chat/completions"
KEY = Path(os.environ["NVKEY"]).read_text(encoding="utf-8-sig").strip()

PROMPT = (
    "Сегодня 2026-08-03. Верни только JSON {intent,title,start,duration_min}. "
    "Фраза: Английский завтра в 12:00 на два часа"
)


def one(model, i):
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": PROMPT}],
        }
    ).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            r.read()
        return "ok", time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read()[:160].decode(errors="replace")
        tag = f"{e.code}"
        if "ResourceExhausted" in raw:
            tag = "503-ResourceExhausted(shared)"
        elif e.code == 429:
            tag = "429-RateLimit(per-key)"
        elif e.code == 529:
            tag = "529-Overloaded(shared)"
        return tag, time.time() - t0
    except Exception as e:
        return type(e).__name__, time.time() - t0


def run(model, n, conc):
    with ThreadPoolExecutor(max_workers=conc) as ex:
        res = list(ex.map(lambda i: one(model, i), range(n)))
    c = collections.Counter(r[0] for r in res)
    oks = sorted(t for s, t in res if s == "ok")
    p50 = oks[len(oks) // 2] if oks else float("nan")
    p95 = oks[int(len(oks) * 0.95) - 1] if oks else float("nan")
    print(
        f"{model:40s} conc={conc:<2d} n={n:<3d} ok={c['ok']:<3d} "
        f"p50={p50:5.1f}s p95={p95:5.1f}s  {dict(c)}"
    )
    return c


if __name__ == "__main__":
    for model in [
        "nvidia/nemotron-3-super-120b-a12b",
        "z-ai/glm-5.2",
        "nvidia/nvidia-nemotron-nano-9b-v2",
    ]:
        print(f"\n=== {model} ===")
        run(model, 12, 1)  # последовательно — реальный профиль одного пользователя
        run(model, 24, 8)  # всплеск
        run(model, 40, 16)  # жёсткий всплеск
