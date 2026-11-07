"""Capy Kondo - the ASI:One-facing marketplace agent (Fetch.ai uAgent).

Buyers: discover items, see real-photo evidence, haggle (floor enforced in code), pay (Payment Protocol + Stripe).
Sellers: hand clutter photos to the Capy Appraiser agent, correct listings (Capy's Brain learns), check sales.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, StartSessionContent, TextContent, chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment, CommitPayment, CompletePayment, Funds, RejectPayment, RequestPayment, payment_protocol_spec,
)

from .. import brain, commerce, config, llm, payments, store, subq
from .common import card_msg, money, now, parse_selection, seed_for, text_msg
from .protocols import AppraiseRequest, AppraiseResult, make_appraisal_protocol

log = logging.getLogger("capy.broker")
README = config.ROOT / "capykondo" / "agents" / "README_broker.md"

agent = Agent(
    name="capy-kondo",
    seed=seed_for("broker", config.BROKER_SEED),
    port=config.BROKER_PORT,
    mailbox=True,
    readme_path=str(README),
    description=("Capy Kondo: a calm capybara that turns your clutter into listings and sells it for you. "
                 "Find secondhand items with real-photo evidence, haggle, and pay inside ASI:One."),
    publish_agent_details=True,
    handle_messages_concurrently=True,
)
chat = Protocol(spec=chat_protocol_spec)
appraisal_protocol = make_appraisal_protocol("requester")
pay = Protocol(spec=payment_protocol_spec, role="seller")

APPRAISER_ADDRESS: dict[str, str] = {}   # filled by runtime with the appraiser agent address
SESSIONS: dict[str, str] = {}            # appraisal session id -> chat sender

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["browse", "item", "offer", "buy", "sell", "correct", "brain",
                                              "my_listings", "help", "chitchat"]},
        "query": {"type": "string", "description": "what the buyer is looking for, short"},
        "max_price": {"type": "number", "description": "budget in USD, 0 if none"},
        "item_ref": {"type": "string", "description": "item id, list number (e.g. '2') or name, empty if none"},
        "amount": {"type": "number", "description": "offered or corrected price in USD, 0 if none"},
        "field": {"type": "string", "description": "for corrections: title|brand|category|condition|price, else empty"},
        "value": {"type": "string", "description": "for corrections: the corrected value, else empty"},
        "media_url": {"type": "string", "description": "http(s) URL of a photo/video in the message, else empty"},
    },
    "required": ["intent", "query", "max_price", "item_ref", "amount", "field", "value", "media_url"],
    "additionalProperties": False,
}

HELP = ("I'm **Capy Kondo** 🍊 - I help people make room for what matters by selling their clutter.\n\n"
        "**Buying?** Ask me things like *\"any headphones under $30?\"* or *\"show me what's for sale\"*. "
        "Every listing uses the item's real camera pixels, an AI condition check, and live price comps. "
        "You can haggle - I'm fair.\n\n"
        "**Selling?** Send me a photo or video link of your clutter (or snap one at {scan_url}). "
        "My partner agent, Capy Appraiser, finds every item, cuts it out, prices it, and I list it. "
        "If I get something wrong, just tell me (*\"the mug should be $5\"*) - I learn from it, "
        "but only keep lessons that prove themselves on held-out examples.")


# ------------------------------------------------------------------ state helpers

def _state(ctx: Context, sender: str) -> dict:
    raw = ctx.storage.get(f"s:{sender}")
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _save(ctx: Context, sender: str, st: dict) -> None:
    ctx.storage.set(f"s:{sender}", json.dumps(st))


def _resolve(ref: str, st: dict) -> dict | None:
    ref = (ref or "").strip().lower()
    last = st.get("results") or []
    if ref.startswith("item_"):
        return store.get_item(ref)
    m = re.fullmatch(r"#?(\d{1,2})(st|nd|rd|th)?", ref)
    if m and last:
        i = int(m.group(1)) - 1
        if 0 <= i < len(last):
            return store.get_item(last[i])
    words = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "last": -1}
    for w, i in words.items():
        if w in ref and last:
            return store.get_item(last[i])
    if ref in ("it", "that", "this", "that one", "this one", "") and st.get("item"):
        return store.get_item(st["item"])
    return commerce.resolve_item(ref)


# ------------------------------------------------------------------ cards

def _subtitle(p: dict) -> str:
    bits = [p.get("condition") or "", p.get("category") or ""]
    return " · ".join(b for b in bits if b).title()


def carousel(title: str, items: list[dict], subtitle: str = "") -> dict:
    out = []
    for p in items[:10]:
        badges = [{"label": "Real photo", "variant": "info"}]
        if p.get("market_price") and p.get("price") and p["price"] < p["market_price"]:
            badges.append({"label": "Below market", "variant": "success"})
        if p.get("status") == "reserved":
            badges.append({"label": "Reserved", "variant": "warning"})
        entry = {"id": p["id"], "title": p["title"], "subtitle": _subtitle(p), "badges": badges,
                 "secondary_text": money(p.get("price")),
                 "primary_cta": {"label": "View", "selection": {"action": "view_item", "item_id": p["id"]}}}
        img = p.get("image_url") or p.get("image")  # public URL for ASI:One; relative path for the local console
        if img:
            entry["image"] = img
        out.append(entry)
    return {"title": title, "subtitle": subtitle, "items": out}


def detail(p: dict) -> dict:
    rows = [{"label": "Price", "value": money(p.get("price"))},
            {"label": "Condition", "value": (p.get("condition") or "unknown").title()}]
    if p.get("flaws"):
        rows.append({"label": "Visible flaws", "value": "; ".join(p["flaws"][:3])})
    if p.get("market_price"):
        rows.append({"label": "Market estimate", "value": money(p["market_price"])})
    if p.get("comps"):
        c = p["comps"][0]
        rows.append({"label": "Price comp", "value": f"{c.get('source', 'listing')}: {money(c.get('price'))}"})
    t = p.get("trust") or {}
    if t:
        bits = []
        if t.get("photo_matches") is not None:
            bits.append(f"photo matches {round(100 * t['photo_matches'])}%")
        if t.get("brand_visible") is not None:
            bits.append(f"brand visible {round(100 * t['brand_visible'])}%")
        if "brand_unverified" in (t.get("flags") or []):
            bits.append("brand claim removed")
        rows.append({"label": "Verified by SubQ Snap", "value": ", ".join(bits) or t.get("verdict", "")})
    if p.get("real_photo_url"):
        rows.append({"label": "Unedited photo", "value": p["real_photo_url"]})
    payload = {"title": p["title"], "summary_rows": rows,
               "ctas": [{"label": f"Buy for {money(p.get('price'))}", "primary": True,
                         "selection": {"action": "buy", "item_id": p["id"]}},
                        {"label": "Make an offer", "selection": {"action": "offer_form", "item_id": p["id"]}},
                        {"label": "Back to results", "selection": {"action": "browse"}}]}
    if p.get("image_url") or p.get("image"):
        payload["hero_image"] = p.get("image_url") or p.get("image")
    return payload


def offer_form(p: dict) -> dict:
    return {"title": f"Offer on {p['title']}",
            "fields": [{"name": "amount", "kind": "number", "label": f"Your offer in USD (listed {money(p['price'])})",
                        "required": True}],
            "submit_cta": {"label": "Send offer", "selection": {"action": "offer", "item_id": p["id"]}}}


def review(p: dict, amount: float) -> dict:
    return {"title": "Confirm your purchase",
            "summary_rows": [{"label": "Item", "value": p["title"]},
                             {"label": "Price", "value": money(amount)},
                             {"label": "Condition", "value": (p.get("condition") or "").title()},
                             {"label": "Handoff", "value": "Local pickup with a code"},
                             {"label": "Payment", "value": "Card via Stripe (test mode)" if payments.enabled()
                              else "Demo payment - no real money moves"}],
            "approve_cta": {"label": f"Pay {money(amount)}", "primary": True,
                            "selection": {"action": "confirm_buy", "item_id": p["id"], "amount": str(amount)}},
            "reject_cta": {"label": "Not now", "selection": {"action": "cancel"}}}


# ------------------------------------------------------------------ handlers

async def classify(text: str, st: dict) -> dict:
    last = []
    for i, iid in enumerate((st.get("results") or [])[:8], start=1):
        it = store.get_item(iid)
        if it:
            last.append(f"{i}. {it.get('title')} ({money(it.get('price'))}) id={iid}")
    prompt = ("Route this message for Capy Kondo, a capybara who sells people's decluttered secondhand items "
              "and helps sellers list theirs. Intents: browse (buyer looking for items), item (asks about one item), "
              "offer (proposes a specific price), buy (wants it at list price), sell (wants to declutter/sell "
              "their own stuff), correct (seller fixes one of their listings), brain (asks what Capy learned / "
              "how it improves), my_listings (seller asks about their listings or sales), help, chitchat.\n"
              f"Items the user last saw:\n{chr(10).join(last) or '(none)'}\n"
              f"Focused item: {st.get('item') or '(none)'}\nMessage: {text}")
    try:
        out, _ = await llm.muse_json([{"type": "input_text", "text": prompt}], INTENT_SCHEMA, name="route",
                                     effort="minimal")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("intent fallback: %s", e)
        t = text.lower()
        intent = "sell" if any(w in t for w in ("sell my", "declutter", "list my")) else "browse"
        m = re.search(r"https?://\S+", text)
        return {"intent": intent, "query": text, "max_price": 0, "item_ref": "", "amount": 0, "field": "",
                "value": "", "media_url": m.group(0) if m else ""}


async def send_browse(ctx: Context, sender: str, st: dict, query: str = "", max_price: float = 0) -> None:
    results, info = await commerce.smart_search(query, max_price or None)
    note = ""
    if info.get("engine") == "subq":
        store.log_activity("subq", what="shelf search", query=query, scored=info.get("scored"),
                           top=[(store.get_item(i) or {}).get("title", i) + f" {p:.2f}" for i, p in info.get("top", [])])
    if not results and (query or max_price):
        results = commerce.search("", max_price or None) or commerce.search("")
        note = "I couldn't find an exact match, but here's what's on the shelf right now. "
    if not results:
        await ctx.send(sender, text_msg("The shelf is empty right now - check back soon! 🍊"))
        return
    st["results"] = [r["id"] for r in results]
    _save(ctx, sender, st)
    lines = "\n".join(f"{i}. **{r['title']}** - {money(r['price'])} ({(r.get('condition') or '').lower()})"
                      for i, r in enumerate(results, start=1))
    head = f"{note}Here {'is' if len(results) == 1 else 'are'} {len(results)} item{'s' if len(results) != 1 else ''}"
    head += f" under {money(max_price)}" if max_price else ""
    text = f"{head}:\n\n{lines}\n\nTap one to see its unedited photo, condition notes and price comps."
    await ctx.send(sender, card_msg(text, "carousel", carousel("Capy's shelf", results,
                                                                f"{len(results)} real items, real photos")))


async def send_item(ctx: Context, sender: str, st: dict, it: dict | None) -> None:
    if not it or it["status"] not in ("listed", "reserved"):
        await ctx.send(sender, text_msg("Hmm, I can't find that one - it may have sold. Want to see what's left?"))
        return
    st["item"] = it["id"]
    _save(ctx, sender, st)
    p = commerce.public_item(it)
    flaws = f" Visible flaws: {'; '.join(p['flaws'][:3])}." if p.get("flaws") else ""
    comps = ""
    if p.get("comps"):
        comps = " Comparable listings: " + ", ".join(f"[{c.get('source') or 'listing'} {money(c.get('price'))}]({c['url']})"
                                                      for c in p["comps"][:3]) + "."
    text = (f"**{p['title']}** - {money(p['price'])}, condition: {p.get('condition')}.{flaws}{comps} "
            f"{p.get('description') or ''}")
    await ctx.send(sender, card_msg(text, "detail", detail(p)))


async def send_review(ctx: Context, sender: str, st: dict, it: dict, amount: float) -> None:
    st["item"] = it["id"]
    st["agreed"] = {it["id"]: amount}
    _save(ctx, sender, st)
    p = commerce.public_item(it)
    await ctx.send(sender, card_msg(f"Great choice! {p['title']} for {money(amount)}. Ready to pay?", "review",
                                    review(p, amount)))


async def handle_offer(ctx: Context, sender: str, st: dict, it: dict | None, amount: float) -> None:
    if not it:
        await ctx.send(sender, text_msg("Which item is your offer for?"))
        return
    if amount <= 0:
        await ctx.send(sender, card_msg(f"What would you like to offer for {it['title']}?", "form",
                                        offer_form(commerce.public_item(it))))
        return
    res = await commerce.offer(it["id"], sender, amount)
    if not res.get("ok"):
        await ctx.send(sender, text_msg(res["message"]))
        return
    if res["decision"] == "accept":
        await send_review(ctx, sender, st, it, res["amount"])
        return
    st["item"] = it["id"]
    _save(ctx, sender, st)
    p = commerce.public_item(it)
    payload = {"title": f"Counter-offer: {money(res['amount'])}",
               "summary_rows": [{"label": "Item", "value": p["title"]},
                                {"label": "Your offer", "value": money(amount)},
                                {"label": "Capy's counter", "value": money(res["amount"])}]
               + ([{"label": "Strategy (SubQ Snap)",
                    "value": f"{res['strategy']['strategy'].replace('_', ' ')} · "
                             f"{round(100 * (res['strategy'].get('confidence') or 0))}% confidence"}]
                  if res.get("strategy") else []),
               "approve_cta": {"label": f"Accept {money(res['amount'])}", "primary": True,
                               "selection": {"action": "accept_counter", "item_id": it["id"],
                                             "amount": str(res["amount"])}},
               "reject_cta": {"label": "Make another offer", "selection": {"action": "offer_form",
                                                                           "item_id": it["id"]}}}
    await ctx.send(sender, card_msg(res["message"], "review", payload))


async def handle_confirm(ctx: Context, sender: str, st: dict, it: dict | None, amount: float | None) -> None:
    if not it or it["status"] != "listed":
        await ctx.send(sender, text_msg("Sorry, that item was just reserved by someone else. 🍊"))
        return
    agreed = (st.get("agreed") or {}).get(it["id"])
    amt = float(agreed if agreed else it["price"])
    if amount and agreed and abs(float(amount) - float(agreed)) > 0.01:
        amt = float(agreed)            # never trust a client-supplied price
    try:
        order = commerce.create_order(it["id"], sender, amt)
    except ValueError:
        await ctx.send(sender, text_msg("Sorry, that item is no longer available."))
        return
    st["order"] = order["order_id"]
    _save(ctx, sender, st)
    co = order["checkout"]
    if co["mode"] == "demo":
        commerce.complete_order(order["order_id"], "demo")
        await send_receipt(ctx, sender, order["order_id"])
        return
    md: dict = {"service": "capy-kondo", "order_id": order["order_id"], "checkout_url": co["url"]}
    if co.get("embedded"):
        md["stripe"] = {k: str(v) for k, v in co["embedded"].items()}
    await ctx.send(sender, RequestPayment(
        accepted_funds=[Funds(currency="USD", amount=f"{order['amount']:.2f}", payment_method="stripe")],
        recipient=str(ctx.agent.address), deadline_seconds=1800, reference=order["order_id"],
        description=f"{order['item']['title']} - {money(order['amount'])}", metadata=md))
    await ctx.send(sender, text_msg(
        f"Reserved for you! Pay {money(order['amount'])} securely here (Stripe test mode, card 4242 4242 4242 4242): "
        f"{co['url']}\n\nI'll confirm as soon as the payment clears."))
    asyncio.create_task(_watch_payment(ctx, sender, order["order_id"]))


async def _watch_payment(ctx: Context, sender: str, order_id: str) -> None:
    for _ in range(300):       # ~15 minutes
        await asyncio.sleep(3)
        o = commerce.get_order(order_id)
        if not o or o["status"] in ("paid", "cancelled"):
            if o and o["status"] == "paid":
                return
            return
        if commerce.check_paid(o):
            commerce.complete_order(order_id)
            try:
                await ctx.send(sender, CompletePayment(transaction_id=o.get("payment_ref") or order_id))
            except Exception:  # noqa: BLE001
                pass
            await send_receipt(ctx, sender, order_id)
            return


async def send_receipt(ctx: Context, sender: str, order_id: str) -> None:
    o = commerce.get_order(order_id)
    it = store.get_item(o["item_id"])
    payload = {"title": "Sold! 🎉", "summary_rows": [
        {"label": "Item", "value": it.get("title")}, {"label": "Paid", "value": money(o["amount"])},
        {"label": "Pickup code", "value": o["pickup_code"]},
        {"label": "Payment", "value": "Stripe (test mode)" if o["payment_method"] != "demo" else "Demo (no real money)"}],
        "ctas": [{"label": "Keep browsing", "selection": {"action": "browse"}}]}
    await ctx.send(sender, card_msg(
        f"It's yours! Show pickup code **{o['pickup_code']}** to the seller. Thanks for giving "
        f"{it.get('title')} a second home. 🍊", "detail", payload))


async def handle_sell(ctx: Context, sender: str, st: dict, media_url: str) -> None:
    scan_url = config.PUBLIC_BASE_URL or f"http://localhost:{config.PORT}"
    if not media_url:
        await ctx.send(sender, text_msg(
            "Let's make some room! 🍊 Send me a link to a photo or short video of the things you want to sell, "
            f"or open {scan_url} on your phone and point the camera at your clutter. Capy Appraiser will find "
            "every item, cut it out with real pixels, check its condition and price it from live listings."))
        return
    session = uuid4().hex
    SESSIONS[session] = sender
    appraiser = APPRAISER_ADDRESS.get("address")
    if not appraiser:
        await ctx.send(sender, text_msg("My appraiser is napping - try again in a moment."))
        return
    await ctx.send(sender, text_msg("On it! I've asked **Capy Appraiser** to look at your photo - finding items, "
                                    "segmenting them with SAM 3.1, checking condition and pricing. ~40 seconds. 🍊"))
    await ctx.send(appraiser, AppraiseRequest(session=session, media_url=media_url, seller=sender))


async def handle_correct(ctx: Context, sender: str, st: dict, intent: dict) -> None:
    it = _resolve(intent.get("item_ref", ""), st)
    field = (intent.get("field") or "").lower()
    value = intent.get("value") or (str(intent.get("amount")) if intent.get("amount") else "")
    if not it or field not in brain.FIELD_TASK or not value:
        await ctx.send(sender, text_msg("Tell me which listing and what to fix, e.g. *\"the mug should be $5\"* "
                                        "or *\"item 2 is a Sony WH-1000XM4\"*."))
        return
    cid = await brain.record_correction(it["id"], field, value)
    await ctx.send(sender, text_msg(f"Updated **{it.get('title')}** ({field} → {value}). Now I'm turning that into a "
                                    "lesson and testing it on held-out examples before I trust it... 🧪"))
    res = await brain.learn(cid)
    if res.get("status") == "adopted":
        msg = (f"✅ Lesson adopted as **playbook v{res['version']}**: \"{res['lesson']}\" - it fixed this case and "
               f"held-out accuracy went {round(100 * res['score_before'])}% → {round(100 * res['score_after'])}% "
               f"on {res['n']} examples it had never seen (commit `{res['commit']}`).")
    elif res.get("status") == "rolled_back":
        msg = (f"↩️ I tried the lesson \"{res['lesson']}\" but rolled it back: {res['reason']} "
               f"(held-out {round(100 * res['score_before'])}% → {round(100 * res['score_after'])}%). "
               "Your fix still applies to this listing.")
    else:
        msg = "Your fix is saved on the listing."
    await ctx.send(sender, text_msg(msg))


async def handle_brain(ctx: Context, sender: str) -> None:
    s = brain.summary()
    rows = []
    for l in s["lessons"][:6]:
        mark = "✅" if l["status"] == "adopted" else ("↩️" if l["status"] == "rolled_back" else "🧪")
        score = ""
        if l.get("score_before") is not None and l.get("score_after") is not None:
            score = f" ({round(100 * l['score_before'])}%→{round(100 * l['score_after'])}%)"
        rows.append({"label": f"{mark} {l['field']}", "value": f"{l['text']}{score}"})
    if not rows:
        rows = [{"label": "No lessons yet", "value": "Correct a listing and I'll learn from it."}]
    text = (f"My playbook is at **v{s['version']}**. Every lesson comes from a seller's correction, and I only keep it "
            f"if it beats the previous playbook on held-out examples ({s['n_cases']} so far) - otherwise I roll it back.")
    await ctx.send(sender, card_msg(text, "detail", {"title": f"Capy's Brain · v{s['version']}", "summary_rows": rows,
                                                     "ctas": [{"label": "Browse items",
                                                               "selection": {"action": "browse"}}]}))


async def handle_my_listings(ctx: Context, sender: str, st: dict) -> None:
    items = [i for i in store.list_items(("listed", "reserved", "sold"))][:10]
    if not items:
        await ctx.send(sender, text_msg("No listings yet - send me a photo of your clutter to get started!"))
        return
    stats = commerce.stats()
    st["results"] = [i["id"] for i in items if i["status"] != "sold"]
    _save(ctx, sender, st)
    pubs = [commerce.public_item(i) for i in items]
    text = (f"You have {stats['listed']} items listed ({money(stats['listed_value'])}), {stats['reserved']} reserved "
            f"and {stats['sold']} sold ({money(stats['revenue'])} earned). Room made: {stats['sold']} thing{'s' if stats['sold'] != 1 else ''}! 🍊")
    await ctx.send(sender, card_msg(text, "carousel", carousel("Your listings", pubs)))


PERSONA = ("You are Capy Kondo, a calm, warm capybara who helps people declutter by selling things they no longer "
           "need, and helps buyers find good secondhand items. Reply in 1-2 short sentences and invite them to "
           "browse or to send a photo of clutter to sell.")


async def chitchat(ctx: Context, sender: str, text: str) -> None:
    reply = ""
    if config.ASI1_API_KEY:  # Fetch.ai's own LLM for small talk
        try:
            from openai import AsyncOpenAI
            asi = AsyncOpenAI(base_url="https://api.asi1.ai/v1", api_key=config.ASI1_API_KEY, timeout=30)
            r = await asi.chat.completions.create(model=config.ASI1_MODEL, max_tokens=160, messages=[
                {"role": "system", "content": PERSONA}, {"role": "user", "content": text}])
            reply = (r.choices[0].message.content or "").strip()
        except Exception as e:  # noqa: BLE001
            log.warning("ASI:One chat failed: %s", e)
    if not reply:
        try:
            reply = await llm.muse_text([{"role": "user", "content": text}], effort="minimal", max_output_tokens=160,
                                        instructions=PERSONA)
        except Exception:  # noqa: BLE001
            reply = "Hi! I'm Capy Kondo. Want to browse secondhand finds, or sell some clutter?"
    await ctx.send(sender, text_msg(reply))


async def dispatch(ctx: Context, sender: str, text: str) -> None:
    st = _state(ctx, sender)
    sel = parse_selection(text)
    if sel and sel.get("action"):
        a = sel["action"]
        it = store.get_item(sel["item_id"]) if sel.get("item_id") else None
        amount = float(sel["amount"]) if str(sel.get("amount", "")).replace(".", "", 1).isdigit() else None
        if a == "view_item":
            return await send_item(ctx, sender, st, it)
        if a == "buy":
            return await send_review(ctx, sender, st, it, float(it["price"])) if it else None
        if a == "offer_form":
            return await handle_offer(ctx, sender, st, it, 0)
        if a == "offer":
            return await handle_offer(ctx, sender, st, it, amount or 0)
        if a == "accept_counter":
            return await send_review(ctx, sender, st, it, amount or float(it["price"])) if it else None
        if a == "confirm_buy":
            return await handle_confirm(ctx, sender, st, it, amount)
        if a == "browse":
            return await send_browse(ctx, sender, st)
        if a == "cancel":
            return await ctx.send(sender, text_msg("No problem - the item stays on the shelf. 🍊"))
    intent = await classify(text, st)
    kind = intent.get("intent")
    if kind == "browse":
        return await send_browse(ctx, sender, st, intent.get("query", ""), float(intent.get("max_price") or 0))
    if kind == "item":
        return await send_item(ctx, sender, st, _resolve(intent.get("item_ref", ""), st))
    if kind == "offer":
        return await handle_offer(ctx, sender, st, _resolve(intent.get("item_ref", ""), st),
                                  float(intent.get("amount") or 0))
    if kind == "buy":
        it = _resolve(intent.get("item_ref", ""), st)
        if it:
            return await send_review(ctx, sender, st, it, float(it["price"]))
        return await send_browse(ctx, sender, st, intent.get("query", ""))
    if kind == "sell":
        m = re.search(r"https?://\S+", text)
        return await handle_sell(ctx, sender, st, intent.get("media_url") or (m.group(0) if m else ""))
    if kind == "correct":
        return await handle_correct(ctx, sender, st, intent)
    if kind == "brain":
        return await handle_brain(ctx, sender)
    if kind == "my_listings":
        return await handle_my_listings(ctx, sender, st)
    if kind == "help":
        return await ctx.send(sender, text_msg(HELP.format(scan_url=config.PUBLIC_BASE_URL or
                                                           f"http://localhost:{config.PORT}")))
    return await chitchat(ctx, sender, text)


# ------------------------------------------------------------------ protocol wiring

@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=now(), acknowledged_msg_id=msg.msg_id))
    text = " ".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if any(isinstance(c, StartSessionContent) for c in msg.content) and not text:
        return
    if any(isinstance(c, EndSessionContent) for c in msg.content) and not text:
        return
    if not text:
        return
    store.log_activity("chat", agent="capy-kondo", sender=sender[:14] + "…", text=text[:140])
    try:
        if subq.enabled() and not text.lstrip().startswith("{"):
            try:
                st = _state(ctx, sender)
                risk = await subq.buyer_risk(text, st.get("history", []))
                st["history"] = (st.get("history", []) + [text[:300]])[-8:]
                _save(ctx, sender, st)
                if risk["scam"] >= 0.6 or risk["abusive"] >= 0.8:
                    store.log_activity("subq", what="blocked message", scam=round(risk["scam"], 2),
                                       abusive=round(risk["abusive"], 2), text=text[:100])
                    await ctx.send(sender, text_msg(
                        "🍊 For everyone's safety I only do deals inside ASI:One - no off-platform payments, gift cards, "
                        "codes or outside links. Happy to help you browse or make an offer here!"))
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("SubQ risk gate skipped: %s", e)
        await dispatch(ctx, sender, text)
    except Exception as e:  # noqa: BLE001
        log.exception("dispatch failed")
        await ctx.send(sender, text_msg(f"Oops, I tripped over a cable: {e}. Could you try that again?"))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


@pay.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    st = _state(ctx, sender)
    order_id = msg.reference or st.get("order")
    o = commerce.get_order(order_id) if order_id else None
    if not o:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id, reason="Unknown order."))
        return
    if o["status"] == "paid" or commerce.check_paid(o, msg.transaction_id):
        commerce.complete_order(order_id, msg.transaction_id)
        await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
        await send_receipt(ctx, sender, order_id)
    else:
        await ctx.send(sender, CancelPayment(transaction_id=msg.transaction_id,
                                             reason="Stripe has not confirmed the payment yet - please finish checkout."))


@pay.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    st = _state(ctx, sender)
    if st.get("order"):
        commerce.cancel_order(st["order"])
    await ctx.send(sender, text_msg("No worries - I put it back on the shelf. 🍊"))


@appraisal_protocol.on_message(AppraiseResult)
async def on_appraised(ctx: Context, sender: str, msg: AppraiseResult):
    chat_sender = SESSIONS.pop(msg.session, None)
    if not chat_sender:
        return
    if not msg.ok:
        await ctx.send(chat_sender, text_msg(f"Capy Appraiser couldn't read that one ({msg.error}). "
                                             "Try a clearer photo with the items spread out?"))
        return
    items = [i for i in store.items_for_scan(msg.scan_id) if i["status"] in ("listed", "reserved")]
    st = _state(ctx, chat_sender)
    st["results"] = [i["id"] for i in items]
    _save(ctx, chat_sender, st)
    pubs = [commerce.public_item(i) for i in items]
    lines = "\n".join(f"{n}. **{p['title']}** - {money(p['price'])} ({p.get('condition')})"
                      for n, p in enumerate(pubs, start=1))
    text = (f"Done in {msg.seconds:.0f}s! I found and listed **{msg.count} items** worth about "
            f"**{money(msg.total)}**:\n\n{lines}\n\nIf anything's off, just tell me (e.g. *\"#2 should be $12\"*) "
            "- I'll fix it and learn from it.")
    await ctx.send(chat_sender, card_msg(text, "carousel", carousel("Your new listings", pubs,
                                                                    "Real pixels · AI condition check · live comps")))


agent.include(chat, publish_manifest=True)
agent.include(pay, publish_manifest=True)
agent.include(appraisal_protocol, publish_manifest=False)
