"""Capy's Brain: seller corrections become versioned lessons that must prove themselves on held-out cases.

Flow for every correction:
  1. Gemini 4 Argon (the coach) turns the correction into one general, reusable lesson.
  2. The lesson is evaluated with vs. without it on held-out cases it has never seen
     (earlier corrections + a curated starter benchmark), plus the case that triggered it.
  3. Adopt only if it fixes the trigger case AND causes no regressions on held-out cases;
     otherwise roll it back. Every decision is a git commit in data/brain (playbook.md, history.md).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import config, llm, store

log = logging.getLogger("capy.brain")

TASKS = ("discovery", "identification", "pricing", "condition")
FIELD_TASK = {"title": "identification", "brand": "identification", "category": "identification",
              "model": "identification", "price": "pricing", "condition": "condition"}
GRADES = ["poor", "fair", "good", "like new", "new"]
SEED_DIR = config.ROOT / "capykondo" / "seed"
REPO = config.BRAIN_DIR


# ------------------------------------------------------------------ git-versioned playbook

def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True, text=True).stdout


def ensure_repo() -> None:
    if (REPO / ".git").exists():
        return
    REPO.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(REPO)], check=True)
    _git("config", "user.name", "Capy Kondo")
    _git("config", "user.email", "capy@capykondo.local")
    _write_playbook()
    (REPO / "history.md").write_text("# Capy's Brain - decision log\n\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "v0: empty playbook")


def adopted(task: str | None = None) -> list[dict]:
    rows = store.query("SELECT * FROM lessons WHERE status='adopted' ORDER BY version")
    return [r for r in rows if task is None or (r.get("field") == task)]


def version() -> int:
    r = store.one("SELECT MAX(version) AS v FROM lessons WHERE status='adopted'")
    return int((r or {}).get("v") or 0)


def _write_playbook() -> None:
    lines = [f"# Capy's playbook (v{version()})", "",
             "Lessons learned from seller corrections. Each one passed a held-out check before adoption.", ""]
    for task in TASKS:
        ls = adopted(task)
        if not ls:
            continue
        lines.append(f"## {task}")
        for l in ls:
            lines.append(f"- (v{l['version']}) {l['text']}")
        lines.append("")
    (REPO / "playbook.md").write_text("\n".join(lines))


def playbook_section(task: str) -> str:
    ls = adopted(task)
    if not ls:
        return ""
    body = "\n".join(f"- {l['text']}" for l in ls)
    return f"\n\nLessons this seller has taught you (apply them when relevant):\n{body}"


def git_log(n: int = 20) -> list[dict]:
    ensure_repo()
    out = _git("log", f"-n{n}", "--pretty=format:%h%x09%ct%x09%s")
    rows = []
    for line in out.splitlines():
        h, ts, msg = line.split("\t", 2)
        rows.append({"hash": h, "ts": int(ts), "message": msg})
    return rows


def _commit(message: str, history_line: str) -> str:
    _write_playbook()
    with open(REPO / "history.md", "a") as f:
        f.write(history_line + "\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", message)
    return _git("rev-parse", "--short", "HEAD").strip()


# ------------------------------------------------------------------ eval cases

def _cases_dir() -> Path:
    p = config.BRAIN_DIR / "cases"
    p.mkdir(parents=True, exist_ok=True)
    return p


def seed_cases() -> list[dict]:
    f = SEED_DIR / "benchmark.json"
    if not f.exists():
        return []
    cases = json.loads(f.read_text())
    for c in cases:
        c["image"] = str(SEED_DIR / c["image"])
        c["source"] = "starter benchmark"
    return cases


def correction_cases() -> list[dict]:
    rows = store.query("SELECT * FROM corrections ORDER BY created_at")
    cases = []
    for r in rows:
        d = r.get("data") or {}
        if not d.get("image") or not Path(d["image"]).exists():
            continue
        task = FIELD_TASK.get(r["field"])
        if not task:
            continue
        cases.append({"id": r["id"], "task": task, "field": r["field"], "image": d["image"],
                      "name": d.get("name", ""), "category": d.get("category", ""),
                      "expected": r["new"],
                      "source": "SubQ verifier" if d.get("source") == "subq" else "seller correction"})
    return cases


# ------------------------------------------------------------------ task runners (what the lesson changes)

PRED_SCHEMA = {
    "identification": {"type": "object", "properties": {
        "title": {"type": "string"}, "brand": {"type": "string"}, "model": {"type": "string"},
        "category": {"type": "string"}}, "required": ["title", "brand", "model", "category"],
        "additionalProperties": False},
    "pricing": {"type": "object", "properties": {"suggested_price": {"type": "number"}, "reason": {"type": "string"}},
                "required": ["suggested_price", "reason"], "additionalProperties": False},
}


def _lessons_text(task: str, extra: str | None, exclude_ids: set[str] = frozenset()) -> str:
    ls = [l["text"] for l in adopted(task) if l["id"] not in exclude_ids]
    if extra:
        ls.append(extra)
    if not ls:
        return ""
    return "\n\nLessons this seller has taught you (apply them when relevant):\n" + "\n".join(f"- {t}" for t in ls)


async def predict(case: dict, extra_lesson: str | None) -> dict:
    task = case["task"]
    hint = f"Item hint: {case.get('name', '')} ({case.get('category', '')})."
    lessons = _lessons_text(task, extra_lesson)
    if task == "pricing":
        prompt = ("Price this secondhand item for a casual seller who wants to declutter quickly. "
                  "Return the suggested quick-sale price in USD. " + hint + lessons)
        out, _ = await llm.muse_json([{"type": "input_text", "text": prompt},
                                      {"type": "input_image", "image_url": llm.data_url(case["image"])}],
                                     PRED_SCHEMA["pricing"], name="price", effort="low")
        return out
    if task == "identification":
        prompt = ("Identify this item for a secondhand listing: title, brand (or 'Unbranded'), model (or 'Unknown') "
                  "and category. " + hint + lessons)
        out, _ = await llm.muse_json([{"type": "input_text", "text": prompt},
                                      {"type": "input_image", "image_url": llm.data_url(case["image"])}],
                                     PRED_SCHEMA["identification"], name="ident", effort="low")
        return out
    if task == "condition":
        from .pipeline import CONDITION_SCHEMA
        out, _ = await llm.gemini_json(
            [{"type": "text", "text": "You are a meticulous, honest resale inspector. Grade the visible condition. "
                                      + hint + lessons},
             {"type": "image", "data": llm.b64(case["image"]), "mime_type": "image/jpeg"}],
            CONDITION_SCHEMA, thinking="low")
        return out
    raise ValueError(task)


def _norm(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def score(case: dict, pred: dict) -> float:
    task, exp = case["task"], case["expected"]
    try:
        if task == "pricing":
            e = float(re.findall(r"\d+(?:\.\d+)?", str(exp))[0])
            p = float(pred.get("suggested_price") or 0)
            return 1.0 if abs(p - e) <= max(2.0, 0.25 * e) else 0.0
        if task == "condition":
            e, p = str(exp).lower().strip(), str(pred.get("condition", "")).lower().strip()
            if e == p:
                return 1.0
            if e in GRADES and p in GRADES and abs(GRADES.index(e) - GRADES.index(p)) == 1:
                return 0.5
            return 0.0
        if task == "identification":
            field = case.get("field") or "title"
            got = " ".join(str(pred.get(k, "")) for k in (["brand", "model", "title"] if field != "category" else ["category"]))
            et, gt = _norm(str(exp)), _norm(got)
            if not et:
                return 0.0
            return 1.0 if len(et & gt) / len(et) >= 0.6 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0
    return 0.0


async def evaluate(cases: list[dict], lesson: str | None) -> list[float]:
    """Run every case with (or without) the candidate lesson. Identification answers are refereed by
    SubQ Snap in one batched decision call; prices and grades are scored by plain rules."""
    sem = asyncio.Semaphore(12)

    async def one(c: dict) -> dict | None:
        async with sem:
            try:
                return await predict(c, lesson)
            except Exception as e:  # noqa: BLE001
                log.warning("eval case %s failed: %s", c.get("id"), e)
                return None

    preds = await asyncio.gather(*[one(c) for c in cases])
    scores = [score(c, p) if p is not None else 0.0 for c, p in zip(cases, preds)]
    from . import subq
    ident = [i for i, c in enumerate(cases) if c["task"] == "identification" and preds[i] is not None]
    if ident and subq.enabled():
        try:
            judged = await subq.judge([{
                "field": cases[i].get("field") or "title", "expected": str(cases[i]["expected"]),
                "predicted": {k: preds[i].get(k) for k in ("title", "brand", "model", "category")}}
                for i in ident])
            for i, p in zip(ident, judged):
                scores[i] = 1.0 if p >= 0.5 else 0.0
        except Exception as e:  # noqa: BLE001
            log.warning("SubQ referee unavailable, using token overlap: %s", e)
    return scores


# ------------------------------------------------------------------ learning from a correction

LESSON_SCHEMA = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "enum": list(TASKS)},
        "lesson": {"type": "string", "description": "one sentence, general and reusable, imperative voice"},
        "rationale": {"type": "string"},
    },
    "required": ["task", "lesson", "rationale"],
}


def _emit(phase: str, **data) -> None:
    store.bus.publish("brain", phase=phase, **data)


async def propose(correction: dict, image: str) -> dict:
    d = correction.get("data") or {}
    task = FIELD_TASK.get(correction["field"], "identification")
    prompt = (
        "You coach Capy, an AI that photographs a person's clutter and lists it for sale. The seller just corrected "
        f"Capy's {correction['field']} for this item. Item: {d.get('name')} ({d.get('category')}). "
        f"Capy said: {correction['old']!r}. Seller says: {correction['new']!r}. "
        f"Seller's note: {d.get('note') or '(none)'}.\n"
        f"Existing lessons for {task}: {[l['text'] for l in adopted(task)] or 'none'}.\n"
        "Write ONE short, general lesson that would have led to the seller's answer and will help with OTHER similar "
        "items in the future (not just this exact item). Avoid restating an existing lesson. "
        f"The task must be '{task}'."
    )
    out, _ = await llm.gemini_json(
        [{"type": "text", "text": prompt}, {"type": "image", "data": llm.b64(image), "mime_type": "image/jpeg"}],
        LESSON_SCHEMA, thinking="medium")
    out["task"] = task
    return out


async def record_correction(item_id: str, field: str, new_value: str, note: str = "", source: str = "seller",
                            apply: bool = True) -> str:
    """Store a correction (from the seller, or automatic from the SubQ verifier) as a future eval case."""
    ensure_repo()
    item = store.get_item(item_id)
    if not item:
        raise KeyError(item_id)
    d = item.get("data") or {}
    old = (d.get("claimed") or {}).get(field) or (d.get("original") or {}).get(field, item.get(field))
    src = config.MEDIA_DIR / str(d.get("white", "")).removeprefix("/media/")
    cid = store.new_id("corr")
    img = _cases_dir() / f"{cid}.jpg"
    if src.exists():
        shutil.copy(src, img)
    store.execute("INSERT INTO corrections VALUES (?,?,?,?,?,?,?,?)",
                  (cid, item_id, time.time(), field, str(old), str(new_value), None,
                   json.dumps({"image": str(img), "name": item.get("name"), "category": item.get("category"),
                               "note": note, "source": source})))
    if apply:  # apply the correction to the listing right away
        if field in ("price", "floor_price"):
            store.update_item(item_id, **{field: float(re.findall(r"\d+(?:\.\d+)?", str(new_value))[0])})
        elif field in ("title", "brand", "category", "condition", "description"):
            store.update_item(item_id, **{field: str(new_value)})
        store.bus.publish("item_ready", item=store.get_item(item_id))
    store.log_activity("correction", item_id=item_id, field=field, old=str(old), new=str(new_value), source=source)
    return cid


_learn_lock = asyncio.Lock()


async def learn(correction_id: str) -> dict:
    async with _learn_lock:
        return await _learn(correction_id)


async def _learn(correction_id: str) -> dict:
    ensure_repo()
    corr = store.one("SELECT * FROM corrections WHERE id=?", (correction_id,))
    image = (corr.get("data") or {}).get("image")
    task = FIELD_TASK.get(corr["field"])
    if not task or not image or not Path(image).exists():
        return {"status": "skipped"}

    _emit("proposing", correction_id=correction_id, field=corr["field"], old=corr["old"], new=corr["new"])
    prop = await propose(corr, image)
    lesson_text = prop["lesson"].strip()
    lid = store.new_id("lesson")
    store.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (lid, time.time(), "evaluating", None, lesson_text, task, correction_id, None, None, None,
                   json.dumps({"rationale": prop.get("rationale")})))
    store.execute("UPDATE corrections SET lesson_id=? WHERE id=?", (lid, correction_id))

    trigger = next((c for c in correction_cases() if c["id"] == correction_id), None)
    held_out = [c for c in correction_cases() if c["id"] != correction_id and c["task"] == task]
    held_out += [c for c in seed_cases() if c["task"] == task]
    _emit("evaluating", lesson_id=lid, lesson=lesson_text, task=task, n_held_out=len(held_out))

    cases = ([trigger] if trigger else []) + held_out
    before, after = await asyncio.gather(evaluate(cases, None), evaluate(cases, lesson_text))
    trig_before, trig_after = (before[0], after[0]) if trigger else (0.0, 1.0)
    hb, ha = (before[1:], after[1:]) if trigger else (before, after)
    regressions = sum(1 for b, a in zip(hb, ha) if a < b)
    n = len(hb)
    sb, sa = (sum(hb) / n if n else 0.0), (sum(ha) / n if n else 0.0)
    adopt = trig_after >= 1.0 and regressions == 0 and sa >= sb
    status = "adopted" if adopt else "rolled_back"
    ver = version() + 1 if adopt else None
    reason = ("fixed the corrected case with no held-out regressions" if adopt else
              ("did not fix the corrected case" if trig_after < 1.0 else
               f"{regressions} held-out regression(s)" if regressions else "held-out score dropped"))
    store.execute("UPDATE lessons SET status=?, version=?, score_before=?, score_after=?, n_eval=?, data=? WHERE id=?",
                  (status, ver, sb, sa, n, json.dumps({
                      "rationale": prop.get("rationale"), "trigger_before": trig_before, "trigger_after": trig_after,
                      "regressions": regressions, "reason": reason,
                      "cases": [{"source": c["source"], "before": b, "after": a}
                                for c, b, a in zip(cases, before, after)]}), lid))
    pct = lambda x: f"{round(100 * x)}%"  # noqa: E731
    if adopt:
        msg = f"v{ver}: adopt [{task}] {lesson_text} (held-out {pct(sb)} -> {pct(sa)}, n={n})"
    else:
        msg = f"rollback [{task}] {lesson_text} ({reason}; held-out {pct(sb)} -> {pct(sa)}, n={n})"
    commit = _commit(msg, f"- {time.strftime('%Y-%m-%d %H:%M')} {msg}")
    result = {"lesson_id": lid, "lesson": lesson_text, "task": task, "status": status, "version": ver,
              "score_before": sb, "score_after": sa, "n": n, "trigger_before": trig_before,
              "trigger_after": trig_after, "regressions": regressions, "reason": reason, "commit": commit}
    _emit("decided", **result)
    store.log_activity("lesson", **result)
    return result


def summary() -> dict:
    ensure_repo()
    lessons = store.query("SELECT * FROM lessons ORDER BY created_at DESC LIMIT 30")
    return {"version": version(), "lessons": lessons, "log": git_log(),
            "n_cases": len(correction_cases()) + len(seed_cases())}
