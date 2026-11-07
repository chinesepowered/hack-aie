"""SubQ Snap 1.1 (Subquadratic Decisions API): fast, calibrated, typed decisions for Capy Kondo.

Principle (from SubQ's own guidance): the model makes the judgment calls code can't; code stays in charge of
what happens next, using thresholds on the returned probabilities.

Used for:
  * trust_check   - does the real photo back up the listing's claims? (brand, condition, match, prohibited)
  * match_catalog - long-context semantic search: one request scores every listing against a buyer's request
  * buyer_risk    - scam / off-platform-payment gating for buyer messages
  * negotiate     - negotiation strategy as a probability distribution (numbers still clamped in code)
  * judge         - referee for Capy's Brain held-out evaluations (many cases in one request)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import time
from pathlib import Path

import httpx
from PIL import Image

from . import config

log = logging.getLogger("capy.subq")
API = "https://api.subq.ai/v1/systemone"
MODEL = os.environ.get("CAPY_SUBQ_MODEL", "subq-snap-1.1")
KEY = os.environ.get("SUBQ_API_KEY", "")
_client: httpx.AsyncClient | None = None


def enabled() -> bool:
    return bool(KEY)


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60)
    return _client


def image_b64(path: str | Path, max_side: int = 1024) -> dict:
    im = Image.open(path).convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return {"mediaType": "image/jpeg", "base64": base64.b64encode(buf.getvalue()).decode()}


async def decide(state, questions: dict, images: list[dict] | None = None) -> tuple[dict, dict]:
    """POST /v1/systemone. Returns (answers, meta)."""
    body: dict = {"model": MODEL, "state": state, "questions": questions}
    if images:
        body["images"] = images[:4]
    last: Exception | None = None
    for attempt in range(4):
        t0 = time.time()
        try:
            r = await _http().post(API, json=body, headers={"Authorization": f"Bearer {KEY}"})
            if r.status_code in (429,) or r.status_code >= 500:
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            if r.status_code >= 400:
                raise RuntimeError(f"SubQ {r.status_code}: {r.text[:300]}")
            data = r.json()
            return data["answers"], {"model": data.get("model", MODEL), "seconds": round(time.time() - t0, 2),
                                     "input_tokens": (data.get("usage") or {}).get("input_tokens")}
        except httpx.HTTPStatusError as e:
            last = e
            await asyncio.sleep(0.8 * (2 ** attempt))
        except Exception as e:  # noqa: BLE001
            last = e
            break
    raise RuntimeError(f"SubQ decision failed: {last}")


# ------------------------------------------------------------------ listing trust check

UNBRANDED = {"", "unbranded", "unknown", "generic", "no brand", "n/a", "none"}
GRADES = ["poor", "fair", "good", "like new", "new"]
_GRADE_KEYS = {"new": "new", "like_new": "like new", "good": "good", "fair": "fair", "poor": "poor"}


async def trust_check(listing: dict, images: list[Path]) -> dict:
    brand = (listing.get("brand") or "").strip()
    questions: dict = {
        "photo_matches": {"type": "noul", "instructions": "Does the photo show the item described by title?",
                          "criteria": {"true": "Same kind of item as the title", "false": "A different item"}},
        "condition": {"type": "choice", "instructions": "Which condition grade best fits what is visible in the photo?",
                      "criteria": {"new": "Sealed or clearly unused", "like_new": "No visible wear",
                                   "good": "Light normal wear", "fair": "Clear wear or minor damage",
                                   "poor": "Damaged, broken or missing parts"}},
        "description_honest": {"type": "score", "instructions": "How well does the photo back up the description?",
                               "criteria": ["Contradicted", "Partly supported", "Fully supported"]},
        "prohibited": {"type": "noul", "instructions": ("Is this something a marketplace should not allow "
                                                        "(weapons, drugs, recalled or hazardous goods, IDs or personal documents)?")},
    }
    if brand.lower() not in UNBRANDED:
        questions["brand_visible"] = {
            "type": "noul",
            "instructions": "Is the claimed brand clearly visible in the photo as a logo or printed name?",
            "criteria": {"true": "A readable logo or brand name appears on the item",
                         "false": "No readable logo or name; the brand is only a guess"}}
    state = {"title": listing.get("title"), "brand": brand or "Unbranded", "claimed_condition": listing.get("condition"),
             "description": listing.get("description"), "category": listing.get("category")}
    answers, meta = await decide(state, questions, [image_b64(p) for p in images[:2]])
    cond = answers.get("condition", {})
    out = {
        "model": meta["model"], "seconds": meta["seconds"],
        "photo_matches": answers.get("photo_matches", {}).get("noul"),
        "brand_visible": answers.get("brand_visible", {}).get("noul") if "brand_visible" in answers else None,
        "condition": _GRADE_KEYS.get(cond.get("choice", ""), cond.get("choice")),
        "condition_probs": cond.get("probabilities"),
        "condition_confidence": cond.get("confidence"),
        "description_score": answers.get("description_honest", {}).get("score"),
        "prohibited": answers.get("prohibited", {}).get("noul"),
    }
    # ---- code decides what happens next
    flags = []
    if out["prohibited"] is not None and out["prohibited"] > 0.5:
        flags.append("prohibited")
    if out["photo_matches"] is not None and out["photo_matches"] < 0.5:
        flags.append("photo_mismatch")
    if out["brand_visible"] is not None and out["brand_visible"] < 0.35:
        flags.append("brand_unverified")
    claimed = (listing.get("condition") or "").lower()
    if (out["condition"] in GRADES and claimed in GRADES and (out["condition_confidence"] or 0) >= 0.5
            and abs(GRADES.index(out["condition"]) - GRADES.index(claimed)) >= 2):
        flags.append("condition_disputed")
    if out["description_score"] is not None and out["description_score"] < 0.8:
        flags.append("description_unsupported")
    out["flags"] = flags
    parts = [p for p in (out["photo_matches"], out["brand_visible"]) if p is not None]
    if out["description_score"] is not None:
        parts.append(out["description_score"] / 2)
    out["trust"] = round(sum(parts) / len(parts), 2) if parts else None
    out["verdict"] = ("blocked" if "prohibited" in flags else
                      "review" if "photo_mismatch" in flags else
                      "verified" if not flags else "verified_with_notes")
    return out


# ------------------------------------------------------------------ long-context catalog search

async def match_catalog(request: str, items: list[dict], max_items: int = 120) -> list[tuple[str, float]]:
    """Score every listing against a buyer's request in one call (one yes/no question per item)."""
    items = items[:max_items]
    if not items:
        return []
    catalog = [{"id": it["id"], "title": it.get("title") or it.get("name"), "category": it.get("category"),
                "price_usd": it.get("price"), "condition": it.get("condition"),
                "description": (it.get("description") or "")[:300]} for it in items]
    questions = {f"m_{i}": {"type": "noul",
                            "instructions": f"Would listing {it['id']} satisfy the buyer's request?",
                            "criteria": {"true": "It is the kind of thing they asked for and fits stated limits",
                                         "false": "Different kind of item or breaks a stated limit"}}
                 for i, it in enumerate(catalog)}
    answers, _ = await decide({"buyer_request": request, "catalog": catalog}, questions)
    scored = [(catalog[i]["id"], float(answers.get(f"m_{i}", {}).get("noul") or 0)) for i in range(len(catalog))]
    return sorted(scored, key=lambda x: -x[1])


# ------------------------------------------------------------------ buyer messages

async def buyer_risk(message: str, history: list[str] | None = None) -> dict:
    questions = {
        "scam": {"type": "noul", "instructions": "Is the buyer attempting a common marketplace scam?",
                 "criteria": {"true": ("Asks to pay or talk off-platform, gift cards, overpayment/refund tricks, "
                                       "verification codes, shipping-agent schemes, suspicious links"),
                              "false": "Normal shopping, questions or haggling"}},
        "abusive": {"type": "noul", "instructions": "Is the message abusive or harassing?"},
    }
    answers, meta = await decide({"message": message, "recent_messages": (history or [])[-6:]}, questions)
    return {"scam": answers["scam"]["noul"], "abusive": answers["abusive"]["noul"], "seconds": meta["seconds"]}


async def negotiate(listing: dict, offer: float, round_no: int, history: list[dict]) -> dict:
    """Strategy decision with probabilities. Code maps the choice to a number and enforces the floor."""
    questions = {
        "strategy": {"type": "choice", "instructions": "How should the seller respond to this offer?",
                     "criteria": {"accept": "Offer is fair for a quick sale",
                                  "counter_meet_middle": "Reasonable offer; meet in the middle",
                                  "counter_firm": "Too low; counter close to the asking price",
                                  "final_offer": "Several rounds already; give the best final price"}},
        "serious_buyer": {"type": "noul", "instructions": "Does the buyer seem likely to complete the purchase?"},
    }
    state = {"item": listing.get("title"), "asking_price": listing.get("price"),
             "market_estimate": (listing.get("data") or {}).get("market_price"),
             "condition": listing.get("condition"), "offer": offer, "round": round_no + 1,
             "previous_rounds": history[-5:], "seller_goal": "declutter quickly at a fair price"}
    answers, meta = await decide(state, questions)
    s = answers["strategy"]
    return {"strategy": s["choice"], "probabilities": s.get("probabilities"), "confidence": s.get("confidence"),
            "serious": answers["serious_buyer"]["noul"], "seconds": meta["seconds"]}


# ------------------------------------------------------------------ Capy's Brain referee

async def judge(cases: list[dict]) -> list[float]:
    """For each {expected, predicted, field} return P(prediction matches the seller's correction)."""
    if not cases:
        return []
    out: list[float] = []
    for start in range(0, len(cases), 100):
        chunk = cases[start:start + 100]
        state = {"cases": [{"case": f"c{i}", "field": c["field"], "seller_says": c["expected"],
                            "model_said": c["predicted"]} for i, c in enumerate(chunk)]}
        questions = {f"c{i}": {"type": "noul",
                               "instructions": f"For case c{i}: does model_said agree with seller_says for that field?",
                               "criteria": {"true": "Same item identity / same grade / price within about 25%",
                                            "false": "Different brand, model, kind of item, grade or price"}}
                     for i in range(len(chunk))}
        answers, _ = await decide(state, questions)
        out += [float(answers.get(f"c{i}", {}).get("noul") or 0) for i in range(len(chunk))]
    return out


def summary_line(t: dict) -> str:
    bits = []
    if t.get("photo_matches") is not None:
        bits.append(f"photo matches {round(100 * t['photo_matches'])}%")
    if t.get("brand_visible") is not None:
        bits.append(f"brand visible {round(100 * t['brand_visible'])}%")
    if t.get("condition"):
        bits.append(f"condition {t['condition']} ({round(100 * (t.get('condition_confidence') or 0))}% conf.)")
    return "SubQ Snap: " + ", ".join(bits)


def dumps(o) -> str:
    return json.dumps(o, default=str)
