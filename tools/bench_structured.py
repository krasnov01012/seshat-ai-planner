"""Раунд 2: поддержка structured output (json_schema / guided_json) + стабильность."""

import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

API = "https://integrate.api.nvidia.com/v1/chat/completions"
KEY = Path(os.environ["NVKEY"]).read_text(encoding="utf-8-sig").strip()

SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["create", "reschedule", "snooze", "complete", "skip", "unknown"],
        },
        "title": {"type": ["string", "null"]},
        "start": {"type": ["string", "null"]},
        "duration_min": {"type": ["integer", "null"]},
        "recurrence_freq": {
            "type": ["string", "null"],
            "enum": ["daily", "weekly", "monthly", None],
        },
        "byweekday": {"type": ["array", "null"], "items": {"type": "string"}},
        "reminders_min_before": {"type": ["array", "null"], "items": {"type": "integer"}},
        "needs_clarification": {"type": "boolean"},
    },
    "required": ["intent", "needs_clarification"],
    "additionalProperties": False,
}

SYSTEM = (
    "Сегодня 2026-08-03 (понедельник), TZ Europe/Moscow. Ты извлекаешь структуру "
    "из русской фразы для планировщика. Если дата или время неоднозначны — "
    "ставь needs_clarification=true. Отвечай только JSON."
)

CASES = [
    "Через два дня в 15:00 собеседование с А2. Напомни за день и за час.",
    "По понедельникам, средам и пятницам тренировка в 19:00",
    "В следующую пятницу утром подать документы",
    "Английский завтра в 12:00 на два часа",
]

MODELS = [
    "nvidia/nemotron-3-super-120b-a12b",
    "z-ai/glm-5.2",
    "openai/gpt-oss-20b",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "meta/llama-3.3-70b-instruct",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
]

MODES = {
    "json_schema": {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "plan", "strict": True, "schema": SCHEMA},
        }
    },
    "json_object": {"response_format": {"type": "json_object"}},
    "guided_json": {"nvext": {"guided_json": SCHEMA}},
}


def call(model, mode, text):
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2500,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
    }
    body.update(MODES[mode])
    req = urllib.request.Request(
        API,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=150) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}", time.time() - t0
    except Exception as e:
        return None, type(e).__name__, time.time() - t0
    lat = time.time() - t0
    txt = d["choices"][0]["message"].get("content") or ""
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0:
        return None, "no-json", lat
    try:
        return json.loads(txt[s : e + 1]), "ok", lat
    except Exception:
        return None, "bad-json", lat


def probe(args):
    model, mode = args
    outs = [call(model, mode, c) for c in CASES]
    ok = sum(1 for o in outs if o[1] == "ok")
    lats = [o[2] for o in outs]
    stat = outs[0][1] if ok == 0 else "ok"
    clar = [o[0].get("needs_clarification") for o in outs if o[0]]
    return model, mode, ok, len(CASES), sum(lats) / len(lats), stat, clar


if __name__ == "__main__":
    jobs = [(m, mo) for m in MODELS for mo in MODES]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for model, mode, ok, n, lat, stat, clar in ex.map(probe, jobs):
            print(f"{model:42s} {mode:12s} {ok}/{n} avg={lat:5.1f}s {stat:10s} clarify={clar}")
