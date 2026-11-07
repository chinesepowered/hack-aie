"""Marketplace logic shared by the web app and the Fetch.ai agents.

The negotiation *numbers* are decided by code (the seller's floor price can never be breached);
Muse only writes the words.
"""
from __future__ import annotations

import json
import logging
import random
import re
import string
import time

from . import config, llm, payments, store

log = logging.getLogger("capy.commerce")


def public_item(item: dict) -> dict:
    """Buyer-safe view of an item (never exposes the floor price)."""
    d = item.get("data") or {}
    img = d.get("studio") or d.get("white") or d.get("cutout")
    return {
        "id": item["id"], "title": item.get("title") or item.get("name"), "price": item.get("price"),
        "currency": item.get("currency") or "USD", "condition": item.get("condition"),
        "category": item.get("category"), "brand": item.get("brand"), "description": item.get("description"),
        "status": item.get("status"), "image": img, "image_url": config.public_url(img) if img else None,
        "real_photo": d.get("context"), "real_photo_url": config.public_url(d["context"]) if d.get("context") else None,
        "flaws": d.get("flaws", []), "condition_evidence": d.get("condition_evidence"),
        "comps": d.get("comps", []), "market_price": d.get("market_price"), "seen": d.get("seen"),
        "trust": _trust_public(d.get("trust")),
    }


def _trust_public(t: dict | None) -> dict | None:
    if not t:
        return None
    return {k: t.get(k) for k in ("photo_matches", "brand_visible", "condition", "condition_confidence",
                                  "description_score", "verdict", "flags", "trust", "model")}


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def search(query: str = "", max_price: float | None = None, limit: int = 8) -> list[dict]:
    items = store.list_items(("listed",))
    q = _tokens(query) - {"a", "an", "the", "any", "some", "for", "me", "i", "want", "looking", "under", "below",
                          "cheap", "something", "anything", "need", "buy", "find", "show", "items", "item", "stuff"}
    scored = []
    for it in items:
        if max_price is not None and float(it.get("price") or 0) > max_price:
            continue
        d = it.get("data") or {}
        hay = _tokens(" ".join([it.get("title") or "", it.get("name") or "", it.get("category") or "",
                                it.get("brand") or "", " ".join(d.get("keywords") or [])]))
        s = len(q & hay) if q else 1
        # light fuzzy match on prefixes (headphone vs headphones)
        if q and not s:
            s = sum(1 for a in q for b in hay if len(a) > 3 and (a.startswith(b[:4]) or b.startswith(a[:4]))) * 0.5
        if q and s == 0:
            continue
        scored.append((s, -float(it.get("price") or 0), it))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [public_item(x[2]) for x in scored[:limit]]


async def smart_search(query: str = "", max_price: float | None = None, limit: int = 8) -> tuple[list[dict], dict]:
    """Semantic search: SubQ Snap scores the whole shelf against the buyer's words in one long-context call.
    Falls back to keyword search. Returns (results, info)."""
    from . import subq
    q = (query or "").strip()
    items = [i for i in store.list_items(("listed",))
             if max_price is None or float(i.get("price") or 0) <= max_price]
    if not q or len(q) < 3 or not subq.enabled() or not items:
        return search(q, max_price, limit), {"engine": "keyword"}
    try:
        scored = await subq.match_catalog(q + (f" (budget ${max_price:g})" if max_price else ""), items)
        by_id = {i["id"]: i for i in items}
        hits = [(iid, p) for iid, p in scored if p >= 0.5][:limit]
        res = [public_item(by_id[iid]) | {"match": round(p, 2)} for iid, p in hits]
        return res, {"engine": "subq", "scored": len(scored), "top": scored[:3]}
    except Exception as e:  # noqa: BLE001
        log.warning("SubQ search failed, keyword fallback: %s", e)
        return search(q, max_price, limit), {"engine": "keyword"}


def resolve_item(ref: str) -> dict | None:
    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.startswith("item_"):
        it = store.get_item(ref)
        return it if it and it["status"] in ("listed", "reserved") else None
    res = search(ref, limit=1)
    return store.get_item(res[0]["id"]) if res else None


# ------------------------------------------------------------------ negotiation

def _rounds(item_id: str, buyer: str) -> int:
    r = store.one("SELECT COUNT(*) AS n FROM offers WHERE item_id=? AND buyer=?", (item_id, buyer))
    return int((r or {}).get("n") or 0)


def decide_offer(price: float, floor: float, amount: float, round_no: int) -> tuple[str, float]:
    """Return (decision, amount) where decision is accept|counter. Never below floor."""
    floor = min(floor, price)
    if amount >= price:
        return "accept", price
    if amount >= floor and (amount >= 0.9 * price or round_no >= 2):
        return "accept", round(amount, 2)
    if round_no >= 2:
        return "counter", float(max(floor, round(floor)))
    target = max(floor, (amount + price) / 2 if amount >= floor else price * 0.92)
    target = max(floor, round(target))
    if target >= price:
        target = price
    return "counter", float(target)


def strategy_to_amount(strategy: str, price: float, floor: float, amount: float) -> tuple[str, float]:
    """Map SubQ's strategy choice to a concrete number. The floor is enforced here, in code."""
    floor = min(floor, price)
    if strategy == "accept":
        return ("accept", round(amount, 2)) if amount >= floor else ("counter", float(max(floor, round(floor))))
    if strategy == "counter_meet_middle":
        target = (amount + price) / 2
    elif strategy == "counter_firm":
        target = price * 0.95
    else:  # final_offer
        target = floor + (price - floor) * 0.25
    target = float(min(price, max(floor, round(target))))
    if target <= amount:
        return "accept", round(max(amount, floor), 2)
    return "counter", target


async def offer(item_id: str, buyer: str, amount: float) -> dict:
    from . import subq
    item = store.get_item(item_id)
    if not item or item["status"] != "listed":
        return {"ok": False, "message": "Sorry, that one is no longer available."}
    price, floor = float(item["price"] or 0), float(item.get("floor_price") or item["price"] or 0)
    rnd = _rounds(item_id, buyer)
    decision, value = decide_offer(price, floor, float(amount), rnd)
    strategy = None
    if subq.enabled() and float(amount) < price:
        try:
            hist = store.query("SELECT amount, status AS decision, counter FROM offers WHERE item_id=? AND buyer=? "
                               "ORDER BY created_at", (item_id, buyer))
            strategy = await subq.negotiate(item, float(amount), rnd, hist)
            decision, value = strategy_to_amount(strategy["strategy"], price, floor, float(amount))
        except Exception as e:  # noqa: BLE001
            log.warning("SubQ negotiation unavailable, rule fallback: %s", e)
    oid = store.new_id("offer")
    store.execute("INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?)",
                  (oid, item_id, time.time(), buyer, float(amount), decision,
                   value if decision == "counter" else None, rnd + 1, None))
    msg = await _offer_message(item, float(amount), decision, value, rnd)
    store.log_activity("offer", item_id=item_id, title=item.get("title"), buyer=_short(buyer), amount=float(amount),
                       decision=decision, value=value,
                       subq=({"strategy": strategy["strategy"], "confidence": strategy.get("confidence"),
                              "serious": strategy.get("serious")} if strategy else None))
    return {"ok": True, "decision": decision, "amount": value, "offer_id": oid, "message": msg,
            "item": public_item(item), "strategy": strategy}


async def _offer_message(item: dict, amount: float, decision: str, value: float, rnd: int) -> str:
    fallback = (f"Deal! ${value:g} for the {item.get('title')}." if decision == "accept"
                else f"I can't go that low, but I can do ${value:g} for the {item.get('title')}.")
    try:
        txt = await llm.muse_text(
            [{"role": "user", "content": (
                f"You are Capy, a calm, friendly capybara who sells a person's decluttered items. A buyer offered "
                f"${amount:g} for '{item.get('title')}' listed at ${float(item['price']):g}. Decision (already made, do "
                f"not change it): {decision} at ${value:g}. Negotiation round {rnd + 1}. Write ONE or TWO short warm "
                "sentences to the buyer stating exactly that decision and price. Never mention a minimum or floor "
                "price. No emojis except at most one capybara-ish one.")}],
            effort="minimal", max_output_tokens=120)
        if f"{value:g}" in txt.replace(",", ""):
            return txt
    except Exception as e:  # noqa: BLE001
        log.warning("offer message fallback: %s", e)
    return fallback


# ------------------------------------------------------------------ orders + payment

def _short(addr: str) -> str:
    return addr if len(addr) < 20 else addr[:10] + "…" + addr[-4:]


def create_order(item_id: str, buyer: str, amount: float | None = None) -> dict:
    item = store.get_item(item_id)
    if not item or item["status"] != "listed":
        raise ValueError("not available")
    amount = float(amount if amount is not None else item["price"])
    amount = max(amount, float(item.get("floor_price") or 0))
    oid = store.new_id("order")
    code = "CAPY-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    img = (item.get("data") or {}).get("studio")
    checkout = payments.create_checkout(oid, item.get("title") or item.get("name"), amount, buyer,
                                        config.public_url(img) if img else None)
    store.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (oid, item_id, time.time(), buyer, amount, "pending", checkout["mode"],
                   checkout.get("hosted_session_id") or "", code, json.dumps(checkout)))
    store.update_item(item_id, status="reserved")
    store.bus.publish("item_ready", item=store.get_item(item_id))
    store.log_activity("reserved", item_id=item_id, title=item.get("title"), buyer=_short(buyer), amount=amount)
    return {"order_id": oid, "amount": amount, "checkout": checkout, "item": public_item(item), "pickup_code": code}


def get_order(order_id: str) -> dict | None:
    return store.one("SELECT * FROM orders WHERE id=?", (order_id,))


def check_paid(order: dict, session_id: str | None = None) -> bool:
    if order["payment_method"] == "demo":
        return True
    d = order.get("data") or {}
    ids = [i for i in (session_id, order.get("payment_ref"), (d.get("embedded") or {}).get("checkout_session_id")) if i]
    return any(payments.is_paid(i) for i in ids)


def complete_order(order_id: str, payment_ref: str | None = None) -> dict:
    order = get_order(order_id)
    if not order:
        raise KeyError(order_id)
    if order["status"] != "paid":
        store.execute("UPDATE orders SET status='paid', payment_ref=COALESCE(?, payment_ref) WHERE id=?",
                      (payment_ref, order_id))
        item = store.update_item(order["item_id"], status="sold",
                                 data={"sold_to": _short(order["buyer"]), "sold_price": order["amount"],
                                       "sold_at": time.time(), "order_id": order_id})
        store.bus.publish("item_ready", item=item)
        store.bus.publish("sold", item=item, amount=order["amount"], buyer=_short(order["buyer"]))
        store.log_activity("sold", item_id=order["item_id"], title=item.get("title"), amount=order["amount"],
                           buyer=_short(order["buyer"]), method=order["payment_method"])
    return get_order(order_id)


def cancel_order(order_id: str) -> None:
    order = get_order(order_id)
    if order and order["status"] == "pending":
        store.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
        store.update_item(order["item_id"], status="listed")
        store.bus.publish("item_ready", item=store.get_item(order["item_id"]))


def stats() -> dict:
    rows = store.query("SELECT status, COUNT(*) AS n, COALESCE(SUM(price),0) AS v FROM items GROUP BY status")
    by = {r["status"]: r for r in rows}
    sold = store.one("SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS v FROM orders WHERE status='paid'") or {}
    return {
        "listed": int((by.get("listed") or {}).get("n") or 0),
        "listed_value": float((by.get("listed") or {}).get("v") or 0),
        "reserved": int((by.get("reserved") or {}).get("n") or 0),
        "sold": int(sold.get("n") or 0), "revenue": float(sold.get("v") or 0),
        "payments": payments.mode(),
    }
