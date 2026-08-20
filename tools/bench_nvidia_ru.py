"""Бенчмарк моделей NVIDIA NIM на разборе русских фраз планировщика в JSON."""

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

TODAY = "2026-08-03"  # понедельник

SYSTEM = """Ты парсер планов для телеграм-планера. Сегодня 2026-08-03, понедельник, таймзона Europe/Moscow.
Верни ТОЛЬКО JSON без пояснений и без markdown-обёртки, по схеме:
{
  "intent": "create" | "reschedule" | "snooze" | "complete" | "skip" | "unknown",
  "kind": "event" | "task" | "routine" | null,
  "title": string | null,
  "start": "YYYY-MM-DDTHH:MM" | null,
  "due": "YYYY-MM-DDTHH:MM" | null,
  "duration_min": number | null,
  "recurrence": {"freq":"daily"|"weekly"|"monthly"|null,"byweekday":["mon","tue","wed","thu","fri","sat","sun"]|null,"interval":number|null} | null,
  "reminders_min_before": [number] | null,
  "snooze_min": number | null,
  "target_ref": string | null,
  "confidence": number
}
Поля, которых нет в фразе, ставь null. Даты считай от 2026-08-03."""

CASES = [
    {
        "id": "event_tomorrow",
        "user": "Завтра в 15:00 собеседование",
        "expect": {"intent": "create", "kind": "event", "start": "2026-08-04T15:00"},
    },
    {
        "id": "routine_daily",
        "user": "Каждый день в 8:00 принимать добавки",
        "expect": {"intent": "create", "kind": "routine", "freq": "daily", "time": "08:00"},
    },
    {
        "id": "routine_mwf",
        "user": "По понедельникам, средам и пятницам тренировка в 19:00",
        "expect": {
            "intent": "create",
            "kind": "routine",
            "freq": "weekly",
            "byweekday": ["mon", "wed", "fri"],
            "time": "19:00",
        },
    },
    {
        "id": "task_deadline",
        "user": "Сегодня до 20:00 закончить README",
        "expect": {"intent": "create", "kind": "task", "due": "2026-08-03T20:00"},
    },
    {
        "id": "focus_duration",
        "user": "Английский завтра в 12:00 на два часа",
        "expect": {"intent": "create", "start": "2026-08-04T12:00", "duration_min": 120},
    },
    {
        "id": "event_multi_reminders",
        "user": "Через два дня в 15:00 собеседование с А2. Напомни за день и за час.",
        "expect": {
            "intent": "create",
            "kind": "event",
            "start": "2026-08-05T15:00",
            "reminders": [1440, 60],
        },
    },
    {
        "id": "snooze_ctx",
        "user": "Через 20 минут",
        "ctx": "Активное напоминание: «Английский, 2 часа» в 12:00.",
        "expect": {"intent": "snooze", "snooze_min": 20},
    },
    {
        "id": "reschedule",
        "user": "Перенеси собеседование на 16:00",
        "expect": {"intent": "reschedule", "start_time": "16:00"},
    },
    {
        "id": "ambiguous_next_friday",
        "user": "В следующую пятницу утром подать документы",
        "expect": {"intent": "create", "start_date": "2026-08-14"},  # след. пятница, не 07.08
    },
    {
        "id": "already_done",
        "user": "я уже сделал",
        "ctx": "Активное напоминание: «Принять добавки» в 08:00.",
        "expect": {"intent": "complete"},
    },
]

MODELS = [
    "meta/llama-3.3-70b-instruct",
    "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-nano-3-30b-a3b",
    "nvidia/nvidia-nemotron-nano-9b-v2",
    "mistralai/mistral-large-2-instruct",
    "mistralai/mistral-medium-3.5-128b",
    "deepseek-ai/deepseek-v4-flash",
    "z-ai/glm-5.2",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "google/gemma-4-31b-it",
    "moonshotai/kimi-k2.6",
    "minimaxai/minimax-m3",
]


def call(model, case):
    msgs = [{"role": "system", "content": SYSTEM}]
    u = case["user"]
    if case.get("ctx"):
        u = case["ctx"] + "\nСообщение пользователя: " + u
    msgs.append({"role": "user", "content": u})
    body = json.dumps(
        {"model": model, "messages": msgs, "temperature": 0, "max_tokens": 3000}
    ).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {
            "err": f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}",
            "lat": time.time() - t0,
        }
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}", "lat": time.time() - t0}
    lat = time.time() - t0
    msg = d["choices"][0]["message"]
    txt = msg.get("content") or ""
    usage = d.get("usage", {})
    return {"raw": txt, "lat": lat, "usage": usage, "reasoning": bool(msg.get("reasoning_content"))}


def extract_json(txt):
    if not txt:
        return None
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    txt = re.sub(r"^.*?</think>", "", txt, flags=re.S)
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, flags=re.S)
    if m:
        txt = m.group(1)
    s, e = txt.find("{"), txt.rfind("}")
    if s < 0 or e < 0:
        return None
    try:
        return json.loads(txt[s : e + 1])
    except Exception:
        return None


def norm_dt(v):
    return (v or "").replace(" ", "T")[:16]


def score(case, obj):
    """Возвращает (набранные_баллы, максимум, список_ошибок)."""
    if obj is None:
        return 0, 1, ["не JSON"]
    exp, pts, mx, errs = case["expect"], 0, 0, []

    def chk(name, ok, got):
        nonlocal pts, mx
        mx += 1
        if ok:
            pts += 1
        else:
            errs.append(f"{name}={got!r}")

    if "intent" in exp:
        chk("intent", obj.get("intent") == exp["intent"], obj.get("intent"))
    if "kind" in exp:
        chk("kind", obj.get("kind") == exp["kind"], obj.get("kind"))
    if "start" in exp:
        chk("start", norm_dt(obj.get("start")) == exp["start"], obj.get("start"))
    if "due" in exp:
        chk("due", norm_dt(obj.get("due")) == exp["due"], obj.get("due"))
    if "duration_min" in exp:
        chk("dur", obj.get("duration_min") == exp["duration_min"], obj.get("duration_min"))
    if "snooze_min" in exp:
        chk("snooze", obj.get("snooze_min") == exp["snooze_min"], obj.get("snooze_min"))
    if "freq" in exp:
        r = obj.get("recurrence") or {}
        chk("freq", r.get("freq") == exp["freq"], r.get("freq"))
    if "byweekday" in exp:
        r = obj.get("recurrence") or {}
        got = [str(x)[:3].lower() for x in (r.get("byweekday") or [])]
        chk("byweekday", got == exp["byweekday"], r.get("byweekday"))
    if "time" in exp:
        s = norm_dt(obj.get("start"))
        chk("time", s.endswith(exp["time"]), obj.get("start"))
    if "reminders" in exp:
        got = sorted(obj.get("reminders_min_before") or [], reverse=True)
        chk(
            "reminders",
            got == sorted(exp["reminders"], reverse=True),
            obj.get("reminders_min_before"),
        )
    if "start_time" in exp:
        s = norm_dt(obj.get("start"))
        chk("start_time", s.endswith(exp["start_time"]), obj.get("start"))
    if "start_date" in exp:
        s = norm_dt(obj.get("start"))
        chk("start_date", s.startswith(exp["start_date"]), obj.get("start"))
    return pts, mx, errs


def run_model(model):
    rows = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(lambda c: call(model, c), CASES))
    for case, res in zip(CASES, results, strict=True):
        if "err" in res:
            rows.append(
                {
                    "case": case["id"],
                    "pts": 0,
                    "max": 1,
                    "lat": res["lat"],
                    "errs": [res["err"][:120]],
                    "obj": None,
                }
            )
            continue
        obj = extract_json(res["raw"])
        p, m, e = score(case, obj)
        rows.append(
            {
                "case": case["id"],
                "pts": p,
                "max": m,
                "lat": res["lat"],
                "errs": e,
                "obj": obj,
                "ct": res["usage"].get("completion_tokens"),
            }
        )
    return model, rows


if __name__ == "__main__":
    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for model, rows in ex.map(run_model, MODELS):
            out[model] = rows
            tp, tm = sum(r["pts"] for r in rows), sum(r["max"] for r in rows)
            lat = sorted(r["lat"] for r in rows)
            print(
                f"{model:45s} {tp:3d}/{tm:<3d} "
                f"med_lat={lat[len(lat) // 2]:5.1f}s max={lat[-1]:5.1f}s"
            )
    with open("bench_results.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n--- детали ошибок ---")
    for m, rows in out.items():
        bad = [(r["case"], r["errs"]) for r in rows if r["errs"]]
        if bad:
            print(f"\n{m}")
            for c, e in bad:
                print(f"   {c:24s} {e}")
